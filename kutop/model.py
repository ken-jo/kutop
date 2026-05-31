"""Domain model for kutop — generic Kubernetes resource snapshots.

Deliberately free of any workload-specific knowledge: no namespace names,
no pod prefixes, no priorities. Workload-specific behaviour is supplied at
runtime by a Profile (see config.py). Keep this module import-light so it can
be reused by both the fetcher and the renderer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ─────────────────────────── unit conversion ────────────────────────────────


def to_mcpu(v: str) -> int:
    """Parse a Kubernetes CPU quantity into millicores. '1' -> 1000, '250m' -> 250."""
    if not v or v in ("-", "<none>"):
        return 0
    v = v.strip()
    if v.endswith("m"):
        try:
            return int(float(v[:-1]))
        except ValueError:
            return 0
    try:
        return int(float(v) * 1000)
    except ValueError:
        return 0


def to_mi(v: str) -> int:
    """Parse a Kubernetes memory quantity into MiB. Handles Ki/Mi/Gi/Ti and SI K/M/G/T."""
    if not v or v in ("-", "<none>"):
        return 0
    v = v.strip()
    units = {
        "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024, "Pi": 1024 ** 3,
        "K": 1000 / (1024 * 1024), "M": 1000 ** 2 / (1024 * 1024),
        "G": 1000 ** 3 / (1024 * 1024), "T": 1000 ** 4 / (1024 * 1024),
    }
    m = re.match(r"^([0-9.]+)\s*([A-Za-z]+)?$", v)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2) or ""
    if unit in units:
        return int(num * units[unit])
    # bare bytes
    return int(num / (1024 * 1024))


def fmt_cpu(mc: int) -> str:
    """Millicores -> human string. 1000 -> '1', 1500 -> '1.5', 250 -> '250m'."""
    if mc >= 1000:
        whole = mc / 1000
        return f"{whole:.0f}" if whole == int(whole) else f"{whole:.1f}"
    return f"{mc}m"


def fmt_mem(mi: int) -> str:
    """MiB -> human string. 7680 -> '7.5Gi', 256 -> '256Mi'."""
    if mi >= 1024:
        gi = mi / 1024
        return f"{gi:.0f}Gi" if gi == int(gi) else f"{gi:.1f}Gi"
    return f"{mi}Mi"


def pct(used: int, cap: int) -> int:
    return int(round(used * 100 / cap)) if cap else 0


def age_seconds(start_time: str, now: Optional[datetime] = None) -> Optional[int]:
    """Seconds elapsed since an ISO8601 ``start_time`` (e.g. a creationTimestamp).

    Returns ``None`` when ``start_time`` is empty or unparseable so callers can
    render a placeholder. ``now`` is injectable for deterministic tests.
    """
    if not start_time:
        return None
    try:
        s = start_time.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    delta = (ref - dt).total_seconds()
    return int(delta) if delta >= 0 else 0


def fmt_age(secs: Optional[int]) -> str:
    """Human age string from seconds: 90061 -> '1d', 5400 -> '1h', 90 -> '1m'.

    Uses the largest single unit (d/h/m/s) like ``kubectl``'s AGE column.
    Returns '-' for ``None`` (unknown start time).
    """
    if secs is None:
        return "-"
    s = max(0, int(secs))
    if s >= 86400:
        return f"{s // 86400}d"
    if s >= 3600:
        return f"{s // 3600}h"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


# ───────────────────────────── data classes ─────────────────────────────────


@dataclass
class Pod:
    name: str
    namespace: str
    node: str = ""
    phase: str = ""                    # Running / Pending / Failed / Succeeded
    ready: str = ""                    # "1/1"
    restarts: int = 0
    cpu_mcpu: int = 0                  # current usage (metrics-server)
    cpu_cap_mcpu: int = 0              # limit (0 = unlimited)
    cpu_req_mcpu: int = 0
    mem_mi: int = 0                    # current usage
    mem_cap_mi: int = 0               # limit
    mem_req_mi: int = 0
    oomkilled: bool = False
    crashloop: bool = False
    last_terminated_reason: str = ""   # latest container terminated/waiting reason
    start_time: str = ""               # ISO creationTimestamp; "" = unknown
    owner_kind: str = ""               # controller kind (StatefulSet/Deployment/…); "" = bare pod
    owner_name: str = ""               # controller name; "" = bare pod
    # Per-pod PVC-backed storage (summed across the pod's bound PVC volumes,
    # sourced from the kubelet /stats/summary like the cluster-wide PVC panel).
    # None = the pod mounts no PVC / usage is unknown -> rendered as '-' so a
    # stateless pod is visually distinct from a 0%-used one.
    storage_used_mi: Optional[int] = None
    storage_cap_mi: int = 0
    terminating: bool = False           # has a deletionTimestamp (being deleted)
    pvc_claims: list = field(default_factory=list)  # PVC claim names this pod mounts
    weight: int = 900                  # ordering weight injected from Profile

    @property
    def cpu_pct(self) -> int:
        return pct(self.cpu_mcpu, self.cpu_cap_mcpu)

    @property
    def mem_pct(self) -> int:
        return pct(self.mem_mi, self.mem_cap_mi)

    @property
    def storage_pct(self) -> Optional[int]:
        """Percent of PVC capacity used; None when no PVC / capacity unknown."""
        if self.storage_used_mi is None or not self.storage_cap_mi:
            return None
        return pct(self.storage_used_mi, self.storage_cap_mi)

    @property
    def age_secs(self) -> Optional[int]:
        """Pod age in seconds (None when start_time is unknown/unparseable)."""
        return age_seconds(self.start_time)


@dataclass
class Node:
    name: str
    role: str = ""
    cpu_mcpu: int = 0
    cpu_cap_mcpu: int = 0
    cpu_req_mcpu: int = 0
    mem_mi: int = 0
    mem_cap_mi: int = 0
    mem_req_mi: int = 0
    pod_count: int = 0
    ready: bool = True

    @property
    def cpu_pct(self) -> int:
        return pct(self.cpu_mcpu, self.cpu_cap_mcpu)

    @property
    def mem_pct(self) -> int:
        return pct(self.mem_mi, self.mem_cap_mi)


@dataclass
class PVC:
    name: str
    namespace: str
    capacity_mi: int = 0
    used_mi: Optional[int] = None       # None = unknown (render as '-'); int = known
    storage_class: str = ""
    pod: str = ""                       # bound pod, for kubelet stats lookup

    @property
    def used_pct(self) -> Optional[int]:
        if self.used_mi is None or not self.capacity_mi:
            return None
        return pct(self.used_mi, self.capacity_mi)


@dataclass
class Event:
    ts_utc: str          # ISO; renderer converts to profile tz
    name: str
    reason: str
    message: str
    count: int = 1
    type: str = "Normal"  # Normal / Warning


@dataclass
class Alert:
    """One active alert from an AlertManager ``/api/v2/alerts`` response."""
    name: str            # labels.alertname
    severity: str = ""   # labels.severity (critical/warning/info/…)
    state: str = ""      # status.state (active/suppressed/…)
    starts_at: str = ""  # ISO startsAt; renderer derives a "since" age
    resource: str = ""   # affected target (pod/pvc/instance/…) from labels


@dataclass
class HealthResult:
    """One workload health-probe scrape result (M3).

    ``fields`` maps a field label to its extracted value (regex group 1). When
    the probe was unreachable, ``ok`` is False and ``error`` carries a short
    reason; ``fields`` is then empty.
    """
    name: str
    ok: bool = False
    fields: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class Summary:
    """Top-of-screen aggregate counters for fast situational awareness."""
    nodes_ready: int = 0
    nodes_total: int = 0
    pods_running: int = 0
    pods_pending: int = 0
    pods_failed: int = 0
    restarts_total: int = 0
    oomkilled_total: int = 0
    warn_events: int = 0
    alerts_firing: int = 0           # from profile alertmanager probe (optional)
    cpu_used_mcpu: int = 0
    cpu_cap_mcpu: int = 0
    mem_used_mi: int = 0
    mem_cap_mi: int = 0


@dataclass
class Snapshot:
    """One refresh cycle's full state, consumed by the renderer."""
    nodes: list[Node] = field(default_factory=list)
    pods: list[Pod] = field(default_factory=list)
    pvcs: list[PVC] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)         # M2 (AlertManager)
    health: list[HealthResult] = field(default_factory=list)  # M3 (health probes)
    summary: Summary = field(default_factory=Summary)
    error: str = ""        # non-empty if the refresh failed; renderer keeps prior frame
