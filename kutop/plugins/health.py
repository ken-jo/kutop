"""Workload-health plugin for kutop (optional, profile-gated).

The health-probe feature — scrape a metrics endpoint, regex-extract fields, and
render them in a compact row — is too domain/workload-specific for the generic
OSS core, so it lives here as a self-contained plugin behind the seam defined in
:mod:`kutop.plugins`. The core (``fetch.py`` + ``render/app.py``) iterates the
plugin registry and never imports this module by name; if this file is deleted
the core still imports and runs (health simply disappears).

What this plugin owns:
  * :class:`HealthPanel` — the panel widget (built on the common :class:`Panel`).
  * the health-specific fetch orchestration: turn the active config's
    ``health_probes`` into a list of :class:`~kutop.model.HealthResult` on the
    snapshot, using the generic HTTP helpers in :mod:`kutop.probes`.
  * activation: enabled iff the config carries ``health_probes`` (a profile
    contributes them — that is the workload-specific config).

This module is only imported when the plugin registry discovers it (which the
core does in both its fetch and render paths), so the textual import of the
shared :class:`Panel` base at module load is paid only when health is actually in
play. No workload literals live here — URLs/regexes/names all come from the
profile YAML.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from ..render.widgets import Panel

#: widget id the health panel mounts under (the app shows/hides it generically).
PANEL_ID = "health_panel"


def _probe_attr(p: Any, key: str, default: Any) -> Any:
    """Read ``key`` from a probe that may be a dict OR an attribute object.

    The unified Config carries health probes as plain ``{name,url,fields}`` dicts,
    but the core Fetcher mirrors them as :class:`~kutop.config.HealthProbe`
    instances (attribute access). Support both shapes transparently.
    """
    if isinstance(p, dict):
        return p.get(key, default)
    return getattr(p, key, default)


def _to_health_probes(config: Any) -> list:
    """Translate the config's ``health_probes`` into probe objects.

    The scrape helpers only need ``.name/.url/.fields``; reuse the
    :class:`~kutop.config.HealthProbe` dataclass for that attribute access.
    Each source probe may be a dict or an attribute object (see :func:`_probe_attr`).
    Empty / malformed -> [] (no scraping at all, so no network is touched).
    """
    from ..config import HealthProbe

    out: list = []
    for p in getattr(config, "health_probes", None) or []:
        try:
            out.append(HealthProbe(
                name=str(_probe_attr(p, "name", "")),
                url=str(_probe_attr(p, "url", "")),
                fields=dict(_probe_attr(p, "fields", {}) or {}),
            ))
        except Exception:
            continue
    return out


class HealthPanel(Panel):
    """Compact workload health row: ``probe: field=value field=value``.

    One line per configured probe; unreachable probes render dim with their
    error. Fed already-scraped :class:`~kutop.model.HealthResult` rows by the
    app (the HTTP scrape happens in the fetch worker). This is how a user adds
    workload-specific signals (block height / sync lag …) WITHOUT code changes —
    they only edit the profile/config ``health_probes``.

    Built on :class:`Panel`, so it shares the common titled/bordered/scrollable
    chrome with the alerts panel and the data-table panels.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(title="HEALTH", **kwargs)

    def update_health(self, results: "list[HealthResult]") -> None:
        self.set_title("HEALTH")
        body = Text()
        if not results:
            body.append("no probes configured", style="dim")
            self.set_body(body)
            return
        for i, hr in enumerate(results):
            if i:
                body.append("\n")
            if not hr.ok:
                body.append(f"● {hr.name}: ", style="dim")
                body.append(hr.error or "unreachable", style="yellow")
                continue
            body.append(f"● {hr.name}: ", style="bold cyan")
            if not hr.fields:
                body.append("(reachable, no fields matched)", style="dim")
                continue
            first = True
            for label, value in hr.fields.items():
                if not first:
                    body.append("  ")
                first = False
                body.append(f"{label}=", style="dim")
                body.append(str(value), style="bold green")
        self.set_body(body)


class HealthPlugin:
    """The health feature, packaged behind the generic core plugin seam.

    Satisfies :class:`kutop.plugins.KutopPlugin` structurally (no inheritance
    needed): ``panel_id`` + ``is_enabled`` + ``fetch`` + ``make_panel``.
    """

    panel_id = PANEL_ID

    def is_enabled(self, config: Any) -> bool:
        """Enabled when the active config carries any health probes."""
        return bool(getattr(config, "health_probes", None))

    def fetch(self, fetcher: Any, snapshot: Any) -> None:
        """Scrape the configured probes onto ``snapshot.health`` (best effort).

        Runs inside the core's existing off-UI-thread fetch worker. Never raises:
        any failure leaves ``snapshot.health`` as the empty default. Uses the
        fetcher's ``_probe_body`` getter so ``/``-prefixed probe URLs route
        through the Kubernetes API-server proxy (kubeconfig auth, no port-forward).
        """
        probes = _to_health_probes(self._config_of(fetcher))
        if not probes:
            return
        try:
            from ..probes import scrape_probes
            getter = getattr(fetcher, "_probe_body", None)
            snapshot.health = scrape_probes(probes, getter=getter)
        except Exception:
            snapshot.health = []

    @staticmethod
    def _config_of(fetcher: Any):
        """The config-like view carrying this plugin's probes for the translator.

        The core fetcher exposes the live ``health_probes`` list directly (set by
        the app from the unified Config), so we wrap it in a tiny shim with the
        attribute the translator reads.
        """
        class _Shim:
            health_probes = list(getattr(fetcher, "health_probes", []) or [])

        return _Shim()

    def make_panel(self) -> Any:
        """Construct the health panel widget the app mounts."""
        return HealthPanel(id=self.panel_id, classes="-hidden")


# Plugin instance discovered by the registry (``getattr(mod, "PLUGIN")``).
PLUGIN = HealthPlugin()
