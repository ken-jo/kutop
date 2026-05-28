"""Optional HTTP probes for kubetop — AlertManager alerts (M2) + workload health (M3).

Both probes are profile/cluster-linked and strictly opt-in: they only run when
the active Profile (or Config) sets ``alertmanager_url`` / ``health_probes``.
Everything here uses ONLY the standard library (``urllib.request`` + ``re``) so
kubetop keeps zero extra runtime dependencies.

Robustness contracts (mirroring fetch.py):
  * Every call has a short timeout and NEVER raises — failures return an empty
    list (alerts) or a ``HealthResult(ok=False, error=…)`` row (health).
  * These functions block on the network, so callers MUST run them off the UI
    thread (the app drives them inside the existing ``@work(thread=True)``
    fetch worker). This module never touches the Textual event loop.
  * No workload-specific literals live here — URLs/regexes come from the Profile
    YAML only. ``--self-test`` never reaches this module (no URL configured).
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Optional

from .model import Alert, HealthResult

_DEFAULT_TIMEOUT = 3.0   # short — never block the refresh cycle for long


def _http_get(url: str, timeout: float) -> Optional[str]:
    """GET ``url`` and return the body text, or ``None`` on any failure.

    Never raises: connection refused, DNS failure, timeout, non-200 — all map
    to ``None`` so an unreachable/unset endpoint simply yields no data.
    """
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if getattr(resp, "status", 200) and resp.status >= 400:
                return None
            raw = resp.read()
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return None


# ── AlertManager alerts (M2) ──────────────────────────────────────────────────

# Label keys, most-specific first, used to name the target an alert is about.
_RESOURCE_LABELS = (
    "pod", "persistentvolumeclaim", "statefulset", "deployment", "daemonset",
    "container", "instance", "node", "service", "namespace",
)


def _alert_resource(labels: dict) -> str:
    """Pick the most pod/resource-like label value to show as the alert target."""
    for key in _RESOURCE_LABELS:
        val = labels.get(key)
        if val:
            return str(val)
    return ""


def fetch_alerts(url: str, timeout: float = _DEFAULT_TIMEOUT,
                 getter=None) -> list[Alert]:
    """Fetch active alerts from an AlertManager ``/api/v2/alerts`` endpoint.

    Expects a JSON list whose items carry ``.labels.alertname``,
    ``.labels.severity`` and ``.status.state``. Returns ``[]`` for an empty/unset
    URL or any failure (unreachable, bad JSON, unexpected shape). Only ``active``
    alerts are surfaced (suppressed/inhibited are filtered out).

    ``getter(url, timeout)`` overrides how the body is fetched (the app passes one
    that routes ``/``-prefixed paths through ``kubectl --raw`` / the API-server
    proxy, so no localhost port-forward is needed); defaults to a direct request.
    """
    if not url:
        return []
    body = (getter or _http_get)(url, timeout)
    if not body:
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[Alert] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        labels = item.get("labels", {}) or {}
        status = item.get("status", {}) or {}
        state = str(status.get("state", "") or "")
        # show firing/active alerts only; skip suppressed/inhibited
        if state and state != "active":
            continue
        name = str(labels.get("alertname", "") or "")
        if not name:
            continue
        out.append(
            Alert(
                name=name,
                severity=str(labels.get("severity", "") or ""),
                state=state or "active",
                starts_at=str(item.get("startsAt", "") or ""),
                resource=_alert_resource(labels),
            )
        )
    return out


# ── workload health probes (M3) ───────────────────────────────────────────────


def scrape_probe(name: str, url: str, fields: dict,
                 timeout: float = _DEFAULT_TIMEOUT, getter=None) -> HealthResult:
    """Scrape one health endpoint and extract ``fields`` via per-field regex.

    ``fields`` maps a display label to a regex; capturing group 1 of the first
    match becomes the displayed value. Unreachable endpoints yield
    ``HealthResult(ok=False, error=…)``; a reachable endpoint with no field
    matches yields ``ok=True`` and an empty ``fields`` dict.

    ``getter(url, timeout)`` overrides the fetch (see :func:`fetch_alerts`); a
    ``/``-prefixed url then goes through the API-server proxy via kubectl.
    """
    if not url:
        return HealthResult(name=name, ok=False, error="no url")
    body = (getter or _http_get)(url, timeout)
    if body is None:
        return HealthResult(name=name, ok=False, error="unreachable")
    extracted: dict = {}
    for label, pattern in (fields or {}).items():
        try:
            m = re.search(str(pattern), body)
        except re.error:
            continue
        if m:
            # group 1 if the pattern captured, else the whole match
            extracted[label] = m.group(1) if m.groups() else m.group(0)
    return HealthResult(name=name, ok=True, fields=extracted)


def scrape_probes(probes: list, timeout: float = _DEFAULT_TIMEOUT,
                  getter=None) -> list[HealthResult]:
    """Scrape every configured :class:`~kubetop.config.HealthProbe` (best effort).

    Each probe is independent — one unreachable endpoint never affects the
    others. Returns one :class:`HealthResult` per probe (order preserved).
    """
    results: list[HealthResult] = []
    for probe in probes or []:
        try:
            results.append(
                scrape_probe(probe.name, probe.url, probe.fields,
                             timeout=timeout, getter=getter)
            )
        except Exception:
            results.append(HealthResult(name=getattr(probe, "name", "?"),
                                        ok=False, error="probe error"))
    return results
