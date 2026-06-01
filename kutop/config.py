"""Unified configuration system for kutop.

Two layers of structure live here:

* :class:`Profile` — workload-specific knowledge (pod ordering, timezone,
  alertmanager, health probes). Shipped as ``profiles/<name>.yaml``. This is the
  ONLY place workload-specific literals (namespaces, pod prefixes, tz, …) live.

* :class:`Config` — the full user-customisable runtime config: which namespaces
  to watch, refresh interval, theme accent, alert thresholds, which panels are
  visible, and which table COLUMNS are shown / in what order. Every option here
  is editable at runtime (the Options modal, key ``o``), persisted to
  ``~/.config/kutop/config.yaml``, and dumpable as an annotated skeleton via
  ``kutop --dump-config``.

Resolution order for a :class:`Config` (later wins):

  1. built-in defaults (this module — :func:`_default_config_dict`)
  2. profile file (``--profile NAME`` -> ``profiles/NAME.yaml``)
  3. user config file (``~/.config/kutop/config.yaml`` or ``--config PATH``)
  4. CLI overrides (namespaces, interval, tz, …)

The core runs fully without any profile or user file — defaults give an
alphabetical, workload-agnostic view. PyYAML is a runtime dependency so the
published CLI can read YAML profiles and ``~/.config/kutop/config.yaml`` when
launched via installers such as ``uvx kutop@latest``.

NO module here imports textual — keep it light so the CLI ``--dump-config`` and
``--self-test`` paths stay cheap and cluster-free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:  # keep core usable without PyYAML
    _HAS_YAML = False

_BUILTIN_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "profiles")
_USER_PROFILE_DIR = os.path.expanduser("~/.config/kutop/profiles")
_LEGACY_KUBETOP_PROFILE_DIR = os.path.expanduser("~/.config/kubetop/profiles")
_LEGACY_KTOP_PROFILE_DIR = os.path.expanduser("~/.config/ktop/profiles")

# User config + legacy state locations.
CONFIG_DIR = os.path.expanduser("~/.config/kutop")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
_STATE_PATH = os.path.join(CONFIG_DIR, "state.json")

# Pre-rename config dirs. The project has used ktop -> kubetop -> kutop names.
# When ~/.config/kutop/config.yaml is absent but an old config exists we copy it
# over on first load so existing users keep their settings.
_LEGACY_KUBETOP_CONFIG_DIR = os.path.expanduser("~/.config/kubetop")
_LEGACY_KUBETOP_CONFIG_PATH = os.path.join(_LEGACY_KUBETOP_CONFIG_DIR, "config.yaml")
_LEGACY_KUBETOP_STATE_PATH = os.path.join(_LEGACY_KUBETOP_CONFIG_DIR, "state.json")
_LEGACY_KTOP_CONFIG_DIR = os.path.expanduser("~/.config/ktop")
_LEGACY_KTOP_CONFIG_PATH = os.path.join(_LEGACY_KTOP_CONFIG_DIR, "config.yaml")
_LEGACY_KTOP_STATE_PATH = os.path.join(_LEGACY_KTOP_CONFIG_DIR, "state.json")


# ─────────────────────────────── Profile ────────────────────────────────────


@dataclass
class HealthProbe:
    """Optional workload health row (M4). Scrapes an HTTP/metrics endpoint."""
    name: str
    url: str
    # regex -> label; each capturing group 1 becomes the displayed value
    fields: dict = field(default_factory=dict)


@dataclass
class Profile:
    name: str = "generic"
    # ordered (prefix, weight); lowest weight sorts first. Empty -> name sort.
    ordering: list = field(default_factory=list)
    namespaces: list = field(default_factory=list)        # default ns when CLI omits
    timezone: str = ""                                     # "" -> local tz
    # threshold percentages for OK/Warn/Crit coloring
    cpu_warn: int = 75
    cpu_crit: int = 90
    mem_warn: int = 80
    mem_crit: int = 92
    pvc_warn: int = 75
    pvc_crit: int = 90
    alertmanager_url: str = ""                             # "" -> alert panel hidden
    health_probes: list = field(default_factory=list)

    def weight_for(self, pod_name: str) -> int:
        for prefix, w in self.ordering:
            if pod_name.startswith(prefix):
                return w
        return 900


def _profile_path(name_or_path: str) -> Optional[str]:
    if os.path.sep in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        return name_or_path if os.path.exists(name_or_path) else None
    for d in (
        _USER_PROFILE_DIR,
        _LEGACY_KUBETOP_PROFILE_DIR,
        _LEGACY_KTOP_PROFILE_DIR,
        _BUILTIN_PROFILE_DIR,
    ):
        p = os.path.join(d, f"{name_or_path}.yaml")
        if os.path.exists(p):
            return p
    return None


def load_profile(name_or_path: Optional[str]) -> Profile:
    """Load a profile by name (user, legacy, then built-in dirs) or path.

    Returns the generic default Profile when name_or_path is falsy or unresolved.
    """
    if not name_or_path:
        return Profile()
    if not _HAS_YAML:
        raise RuntimeError(
            "PyYAML is required to load profiles. Reinstall kutop or run without "
            "--profile for the generic view."
        )
    path = _profile_path(name_or_path)
    if not path:
        raise FileNotFoundError(f"profile not found: {name_or_path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    ordering = [(o["prefix"], int(o["weight"])) for o in raw.get("ordering", [])]
    probes = [
        HealthProbe(name=p["name"], url=p["url"], fields=p.get("fields", {}))
        for p in raw.get("health_probes", [])
    ]
    th = raw.get("thresholds", {})
    return Profile(
        name=raw.get("name", os.path.splitext(os.path.basename(path))[0]),
        ordering=ordering,
        namespaces=raw.get("namespaces", []),
        timezone=raw.get("timezone", ""),
        cpu_warn=th.get("cpu_warn", 75), cpu_crit=th.get("cpu_crit", 90),
        mem_warn=th.get("mem_warn", 80), mem_crit=th.get("mem_crit", 92),
        pvc_warn=th.get("pvc_warn", 75), pvc_crit=th.get("pvc_crit", 90),
        alertmanager_url=raw.get("alertmanager_url", ""),
        health_probes=probes,
    )


# ─────────────────────────── column registry ────────────────────────────────
#
# Each column knows how to render a single cell for a Pod (and, where it makes
# sense, a Node). The renderer builds the live table purely from the ordered
# list of *visible* columns in the Config — show / hide / reorder is data, not
# code. Accessors receive (obj, ctx) where ctx carries threshold + formatting
# helpers so this module needs no import of model/rich at module load.


@dataclass
class ColumnSpec:
    key: str                       # stable id used in config + registry lookup
    label: str                     # header text
    # pod_accessor(pod, ctx) -> Rich Text or str ; ctx is a RenderCtx
    pod_accessor: Callable[[Any, Any], Any]
    # node_accessor(node, ctx) -> cell ; falls back to a dim "·" when absent
    node_accessor: Optional[Callable[[Any, Any], Any]] = None
    default_visible: bool = False
    width: Optional[int] = None    # fixed width; None -> auto / flex
    align: str = "left"            # left | center | right (for header hinting)
    help: str = ""                 # one-line description for --dump-config


# Default ordered set of columns (key, default_visible). Order here defines the
# default table column order; users can reorder/hide via config.
_DEFAULT_COLUMN_ORDER = [
    "name", "cpu", "cpu_pct", "cpu_gauge",
    "mem", "mem_pct", "mem_gauge",
    # per-pod PVC-backed storage (default-visible USE/CAP + gauge, sits right
    # after the mem columns so it reads like another resource lane):
    "storage", "storage_gauge",
    "restarts",
    # opt-in extras (default hidden):
    "storage_pct",
    "node", "namespace", "ready", "phase",
    "cpu_req", "mem_req", "age", "last_reason",
    "owner", "owner_name",
]

SNAPSHOT_DETAIL_LEVELS = ("normal", "wide", "full")
_SNAPSHOT_WIDE_COLUMNS = [
    "name", "namespace", "ready", "phase", "restarts",
    "last_reason", "owner", "owner_name", "node",
    "cpu", "cpu_pct", "mem", "mem_pct", "storage", "storage_pct", "age",
]


# Sort keys. "priority" is the profile-weight default; the rest map to a Pod
# attribute (resolved in the renderer). Any key here is a valid ``sort_key`` and
# is offered in the Options modal + the ``s`` cycle. Keeping this list adjacent
# to the column registry lets the renderer show a ▲/▼ indicator on the matching
# column header.
SORTABLE_KEYS = (
    "priority", "name", "cpu", "mem", "cpu_pct", "mem_pct",
    "restarts", "phase", "node", "namespace", "age", "storage", "owner",
)

# Map a sort_key to the column key whose header should carry the ▲/▼ indicator.
# (cpu/mem usage live under the "cpu"/"mem" columns; *_pct under *_pct, etc.)
SORT_KEY_TO_COLUMN = {
    "name": "name", "cpu": "cpu", "mem": "mem",
    "cpu_pct": "cpu_pct", "mem_pct": "mem_pct", "restarts": "restarts",
    "phase": "phase", "node": "node", "namespace": "namespace", "age": "age",
    "storage": "storage", "owner": "owner",
}

# Map any clickable column key -> the sort_key it should trigger (header-click
# sorting). Gauge/pct variants fold onto their metric; non-sortable columns are
# simply absent (clicking them is a no-op).
COLUMN_TO_SORT_KEY = {
    "name": "name", "cpu": "cpu", "cpu_pct": "cpu_pct", "cpu_gauge": "cpu_pct",
    "mem": "mem", "mem_pct": "mem_pct", "mem_gauge": "mem_pct",
    "storage": "storage", "storage_pct": "storage", "storage_gauge": "storage",
    "restarts": "restarts", "age": "age", "phase": "phase",
    "node": "node", "namespace": "namespace",
    "owner": "owner", "owner_name": "owner",
}


def build_column_registry() -> "dict[str, ColumnSpec]":
    """Construct the column registry.

    Accessors import :mod:`model` and rich locally so importing this module
    stays light (CLI ``--dump-config`` does not need rich/textual). ``ctx`` is a
    small render context (see :class:`RenderCtx`) carrying thresholds + helpers.
    """
    from rich.text import Text
    from . import model

    def _dim_dash():
        return Text("-", style="dim")

    # name -------------------------------------------------------------------
    def c_name_pod(pod, ctx):
        return ctx.pod_name_cell(pod)

    def c_name_node(node, ctx):
        return ctx.node_name_cell(node)

    # cpu usage --------------------------------------------------------------
    def c_cpu_pod(pod, ctx):
        if pod.cpu_cap_mcpu:
            return f"{model.fmt_cpu(pod.cpu_mcpu)}/{model.fmt_cpu(pod.cpu_cap_mcpu)}"
        return model.fmt_cpu(pod.cpu_mcpu)

    def c_cpu_node(node, ctx):
        return f"{model.fmt_cpu(node.cpu_mcpu)}/{model.fmt_cpu(node.cpu_cap_mcpu)}"

    def c_cpu_pct_pod(pod, ctx):
        if not pod.cpu_cap_mcpu:
            return Text("-", style="dim")
        return Text(f"{pod.cpu_pct}%", style=ctx.color(pod.cpu_pct, "cpu"))

    def c_cpu_pct_node(node, ctx):
        return Text(f"{node.cpu_pct}%", style=ctx.color(node.cpu_pct, "cpu"))

    def c_cpu_gauge_pod(pod, ctx):
        return ctx.gauge(pod.cpu_pct if pod.cpu_cap_mcpu else None, "cpu")

    def c_cpu_gauge_node(node, ctx):
        return ctx.gauge(node.cpu_pct, "cpu")

    # mem usage --------------------------------------------------------------
    def c_mem_pod(pod, ctx):
        if pod.mem_cap_mi:
            return f"{model.fmt_mem(pod.mem_mi)}/{model.fmt_mem(pod.mem_cap_mi)}"
        return model.fmt_mem(pod.mem_mi)

    def c_mem_node(node, ctx):
        return f"{model.fmt_mem(node.mem_mi)}/{model.fmt_mem(node.mem_cap_mi)}"

    def c_mem_pct_pod(pod, ctx):
        if not pod.mem_cap_mi:
            return Text("-", style="dim")
        return Text(f"{pod.mem_pct}%", style=ctx.color(pod.mem_pct, "mem"))

    def c_mem_pct_node(node, ctx):
        return Text(f"{node.mem_pct}%", style=ctx.color(node.mem_pct, "mem"))

    def c_mem_gauge_pod(pod, ctx):
        return ctx.gauge(pod.mem_pct if pod.mem_cap_mi else None, "mem")

    def c_mem_gauge_node(node, ctx):
        return ctx.gauge(node.mem_pct, "mem")

    # per-pod PVC-backed storage --------------------------------------------
    # USE/CAP like the cpu/mem columns; '-' when the pod mounts no PVC (so a
    # stateless pod is visually distinct from a 0%-used stateful one). Coloring
    # of the gauge/% uses the PVC thresholds (the same ones that drive the
    # standalone PVC panel) so power users keep one storage threshold to tune.
    def c_storage_pod(pod, ctx):
        # bare '-' only when the pod mounts no PVC at all; a stateful pod whose
        # usage is momentarily unknown still shows '-/CAP'.
        if pod.storage_used_mi is None and not pod.storage_cap_mi:
            return _dim_dash()
        cap = model.fmt_mem(pod.storage_cap_mi) if pod.storage_cap_mi else "-"
        used = (model.fmt_mem(pod.storage_used_mi)
                if pod.storage_used_mi is not None else "-")
        return f"{used}/{cap}"

    def c_storage_pct_pod(pod, ctx):
        sp = pod.storage_pct
        if sp is None:
            return _dim_dash()
        return Text(f"{sp}%", style=ctx.color(sp, "pvc"))

    def c_storage_gauge_pod(pod, ctx):
        return ctx.gauge(pod.storage_pct, "pvc")

    # restarts ---------------------------------------------------------------
    def c_rst_pod(pod, ctx):
        return Text(str(pod.restarts), style="bold red" if pod.restarts else "dim")

    def c_rst_node(node, ctx):
        return Text("NODE", style="dim")

    # opt-in extras ----------------------------------------------------------
    def c_node_pod(pod, ctx):
        return Text(pod.node or "-", style="dim" if not pod.node else "")

    def c_ns_pod(pod, ctx):
        return Text(pod.namespace, style="dim")

    def c_ready_pod(pod, ctx):
        ok = False
        parts = (pod.ready or "").split("/")
        if len(parts) == 2 and parts[0] == parts[1] and parts[0] not in ("", "0"):
            ok = True
        return Text(pod.ready or "-", style="green" if ok else "yellow")

    def c_phase_pod(pod, ctx):
        style = {
            "Running": "green", "Pending": "yellow",
            "Failed": "bold red", "Succeeded": "dim",
        }.get(pod.phase, "")
        return Text(pod.phase or "-", style=style)

    def c_phase_node(node, ctx):
        return Text("Ready" if node.ready else "NotReady",
                    style="green" if node.ready else "bold red")

    def c_cpu_req_pod(pod, ctx):
        return model.fmt_cpu(pod.cpu_req_mcpu) if pod.cpu_req_mcpu else _dim_dash()

    def c_mem_req_pod(pod, ctx):
        return model.fmt_mem(pod.mem_req_mi) if pod.mem_req_mi else _dim_dash()

    def c_age_pod(pod, ctx):
        return ctx.age_cell(pod)

    def c_reason_pod(pod, ctx):
        r = pod.last_terminated_reason
        if pod.oomkilled and not r:
            r = "OOMKilled"
        if pod.crashloop and not r:
            r = "CrashLoop"
        return Text(r, style="bold red") if r else _dim_dash()

    # owner / controller -----------------------------------------------------
    _OWNER_ABBR = {
        "Deployment": "Deploy", "StatefulSet": "STS", "DaemonSet": "DS",
        "ReplicaSet": "RS", "Job": "Job",
    }

    def c_owner_pod(pod, ctx):
        kind = pod.owner_kind
        if not kind:
            return _dim_dash()
        return Text(_OWNER_ABBR.get(kind, kind), style="cyan")

    def c_owner_name_pod(pod, ctx):
        return Text(pod.owner_name, style="dim") if pod.owner_name else _dim_dash()

    specs = [
        ColumnSpec("name", "NODE / POD", c_name_pod, c_name_node,
                   default_visible=True, align="left",
                   help="pod/node name with status glyph + highlights"),
        ColumnSpec("cpu", "CPU", c_cpu_pod, c_cpu_node,
                   default_visible=True, width=11, align="right",
                   help="CPU usage / limit (millicores)"),
        ColumnSpec("cpu_pct", "CPU%", c_cpu_pct_pod, c_cpu_pct_node,
                   default_visible=True, width=6, align="right",
                   help="CPU percent of limit, threshold-colored"),
        ColumnSpec("cpu_gauge", "CPU GAUGE", c_cpu_gauge_pod, c_cpu_gauge_node,
                   default_visible=True, width=12, align="left",
                   help="proportional CPU bar (eighth-block glyphs)"),
        ColumnSpec("mem", "MEM", c_mem_pod, c_mem_node,
                   default_visible=True, width=14, align="right",
                   help="memory usage / limit"),
        ColumnSpec("mem_pct", "MEM%", c_mem_pct_pod, c_mem_pct_node,
                   default_visible=True, width=6, align="right",
                   help="memory percent of limit, threshold-colored"),
        ColumnSpec("mem_gauge", "MEM GAUGE", c_mem_gauge_pod, c_mem_gauge_node,
                   default_visible=True, width=12, align="left",
                   help="proportional memory bar (eighth-block glyphs)"),
        ColumnSpec("storage", "STORAGE", c_storage_pod, None,
                   default_visible=True, width=15, align="right",
                   help="per-pod PVC storage use/cap (- when no PVC)"),
        ColumnSpec("storage_gauge", "STORE GAUGE", c_storage_gauge_pod, None,
                   default_visible=True, width=12, align="left",
                   help="proportional PVC storage bar (PVC thresholds)"),
        ColumnSpec("restarts", "RST", c_rst_pod, c_rst_node,
                   default_visible=True, width=4, align="right",
                   help="container restart count"),
        # opt-in
        ColumnSpec("storage_pct", "STORE%", c_storage_pct_pod, None,
                   default_visible=False, width=7, align="right",
                   help="(opt-in) PVC storage percent of capacity, threshold-colored"),
        ColumnSpec("node", "NODE", c_node_pod, None,
                   default_visible=False, width=18, align="left",
                   help="(opt-in) scheduling node name"),
        ColumnSpec("namespace", "NS", c_ns_pod, None,
                   default_visible=False, width=14, align="left",
                   help="(opt-in) pod namespace"),
        ColumnSpec("ready", "READY", c_ready_pod, None,
                   default_visible=False, width=6, align="right",
                   help="(opt-in) ready containers a/b"),
        ColumnSpec("phase", "PHASE", c_phase_pod, c_phase_node,
                   default_visible=False, width=10, align="left",
                   help="(opt-in) lifecycle phase"),
        ColumnSpec("cpu_req", "CPU REQ", c_cpu_req_pod, None,
                   default_visible=False, width=8, align="right",
                   help="(opt-in) CPU request"),
        ColumnSpec("mem_req", "MEM REQ", c_mem_req_pod, None,
                   default_visible=False, width=9, align="right",
                   help="(opt-in) memory request"),
        ColumnSpec("age", "AGE", c_age_pod, None,
                   default_visible=False, width=6, align="right",
                   help="(opt-in) pod age (requires start time)"),
        ColumnSpec("last_reason", "LAST REASON", c_reason_pod, None,
                   default_visible=False, width=12, align="left",
                   help="(opt-in) last termination reason (OOMKilled/CrashLoop)"),
        ColumnSpec("owner", "OWNER", c_owner_pod, None,
                   default_visible=True, width=8, align="left",
                   help="controller kind (Deploy/STS/DS/RS/Job/…)"),
        ColumnSpec("owner_name", "OWNER NAME", c_owner_name_pod, None,
                   default_visible=False, width=20, align="left",
                   help="(opt-in) controller name (owning workload)"),
    ]
    return {c.key: c for c in specs}


# ─────────────────────────────── Config ─────────────────────────────────────


_VALID_SORT = ("priority", "cpu", "mem", "name")   # legacy sort_mode values
_VALID_ACCENTS = ("cyan", "green", "magenta", "blue", "yellow", "red", "purple")
_VALID_SUMMARY_STYLES = ("tiles", "compact")

# NODE/POD name-column width bounds (cells). The column is mouse-resizable; the
# value is clamped to this range on load and on every drag so the column stays
# usable (never collapses to nothing, never crowds out the metric columns).
NAME_WIDTH_MIN = 12
NAME_WIDTH_MAX = 120
NAME_WIDTH_DEFAULT = 30


def clamp_name_width(v) -> int:
    """Coerce ``v`` to an int and clamp it into the NAME_WIDTH bounds."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        n = NAME_WIDTH_DEFAULT
    return max(NAME_WIDTH_MIN, min(NAME_WIDTH_MAX, n))


def default_visible_columns() -> list:
    """Return the built-in visible table columns in default display order."""
    reg = build_column_registry()
    return [k for k in _DEFAULT_COLUMN_ORDER if reg[k].default_visible]


def snapshot_detail_size(detail: Optional[str]) -> "tuple[int, int]":
    """Default terminal size for screenshot detail presets."""
    return {
        "wide": (160, 44),
        "full": (220, 54),
    }.get(detail or "normal", (140, 40))


def apply_detail_preset(cfg: "Config", detail: Optional[str]) -> "Config":
    """Apply a one-shot detail preset to a runtime config.

    ``normal`` restores the built-in visible columns. ``wide`` and ``full`` are
    intended for screenshots/reviews: they expose diagnostic columns without
    requiring a user config file.
    """
    if not detail:
        return cfg
    if detail not in SNAPSHOT_DETAIL_LEVELS:
        return cfg
    if detail == "normal":
        cfg.columns = default_visible_columns()
        cfg.name_width = NAME_WIDTH_DEFAULT
        return cfg
    if detail == "wide":
        cfg.columns = list(_SNAPSHOT_WIDE_COLUMNS)
        cfg.summary_style = "compact"
        cfg.name_width = 20
        return cfg
    cfg.columns = list(_DEFAULT_COLUMN_ORDER)
    cfg.summary_style = "compact"
    cfg.name_width = 20
    cfg.show_pvc = True
    return cfg


@dataclass
class Config:
    """Full user-customisable runtime config (the visible 'skeleton')."""

    # View
    interval: float = 3.0
    timezone: str = ""                  # "" -> host local
    sort_mode: str = "priority"         # legacy: priority|cpu|mem|name (mirrors sort_key)
    theme_accent: str = "cyan"
    summary_style: str = "tiles"        # tiles | compact
    # Width (in cells) of the NODE/POD name column. The cell content (glyph +
    # name + bracketed annotations) is fit to this width and ellipsised only
    # when it overflows — so a wider column shows more of the name. The user
    # drags the column's right edge to resize it live (persisted here).
    name_width: int = 30               # clamp NAME_WIDTH_MIN..NAME_WIDTH_MAX

    # Sorting (M-iter2): sort by ANY column. sort_key is the active column/key,
    # sort_desc reverses it. priority = profile-weight default.
    sort_key: str = "priority"
    sort_desc: bool = False

    # Filtering / search (M-iter2): adjust which cluster pods are shown.
    name_filter: str = ""               # case-insensitive substring on pod name
    hide_completed: bool = True         # drop Succeeded/Completed pods
    only_problems: bool = False         # show only non-Running / restarts>0 / oom

    # Grouping (M-iter2): cluster topology view.
    group_by_node: bool = False         # group pods under their node header rows

    # Cluster
    namespaces: list = field(default_factory=lambda: ["default"])
    context: str = ""

    # Thresholds (percent)
    cpu_warn: int = 75
    cpu_crit: int = 90
    mem_warn: int = 80
    mem_crit: int = 92
    pvc_warn: int = 75
    pvc_crit: int = 90

    # Panels (visibility)
    show_summary: bool = True
    show_trends: bool = True
    show_podtable: bool = True
    show_events: bool = True
    # PVC is now shown PER POD (storage / storage_gauge columns), so the
    # standalone cluster-wide PVC panel is OFF by default. Power users can still
    # toggle it on (key 'v' / Options > Panels) for a cluster-wide PVC list.
    show_pvc: bool = False
    show_alerts: bool = True            # M2: AlertManager alerts panel
    show_health: bool = True            # M3: workload health row

    # Cluster-linked HTTP probes (M2/M3). Sourced from the profile by default,
    # overridable in the user config. Empty -> the corresponding panel hides.
    alertmanager_url: str = ""          # AlertManager /api/v2/alerts URL
    # health_probes: list of {name, url, fields:{label: regex}} dicts.
    health_probes: list = field(default_factory=list)

    # Columns: ordered list of visible column keys (the rest are hidden).
    columns: list = field(default_factory=list)

    # Profile name that contributed (read-only display).
    profile_name: str = "generic"

    # ── derived helpers ──────────────────────────────────────────────────
    def threshold(self, kind: str) -> "tuple[int, int]":
        """Return (warn, crit) for kind in {cpu, mem, pvc}."""
        return {
            "cpu": (self.cpu_warn, self.cpu_crit),
            "mem": (self.mem_warn, self.mem_crit),
            "pvc": (self.pvc_warn, self.pvc_crit),
        }.get(kind, (self.cpu_warn, self.cpu_crit))

    def visible_columns(self) -> list:
        """Return the ordered list of visible column keys (validated)."""
        reg = build_column_registry()
        out = [k for k in self.columns if k in reg]
        return out or [k for k in _DEFAULT_COLUMN_ORDER
                       if reg[k].default_visible]

    def filters_active(self) -> bool:
        """True if any config-driven filter would drop rows."""
        return bool(self.name_filter) or self.hide_completed or self.only_problems

    # ── (de)serialisation ────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "view": {
                "interval": self.interval,
                "timezone": self.timezone,
                "sort_key": self.sort_key,
                "sort_desc": self.sort_desc,
                "theme_accent": self.theme_accent,
                "summary_style": self.summary_style,
                "group_by_node": self.group_by_node,
                "name_width": self.name_width,
            },
            "cluster": {
                "namespaces": list(self.namespaces),
                "context": self.context,
            },
            "filters": {
                "name_filter": self.name_filter,
                "hide_completed": self.hide_completed,
                "only_problems": self.only_problems,
            },
            "thresholds": {
                "cpu_warn": self.cpu_warn, "cpu_crit": self.cpu_crit,
                "mem_warn": self.mem_warn, "mem_crit": self.mem_crit,
                "pvc_warn": self.pvc_warn, "pvc_crit": self.pvc_crit,
            },
            "panels": {
                "summary": self.show_summary,
                "trends": self.show_trends,
                "podtable": self.show_podtable,
                "events": self.show_events,
                "pvc": self.show_pvc,
                "alerts": self.show_alerts,
                "health": self.show_health,
            },
            "probes": {
                "alertmanager_url": self.alertmanager_url,
                "health_probes": [dict(p) for p in self.health_probes],
            },
            "columns": list(self.columns),
            "profile": self.profile_name,
        }


def _default_config_dict() -> dict:
    """Built-in defaults as a nested dict (layer 1)."""
    return Config(columns=default_visible_columns()).to_dict()


def _coerce_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto ``base`` (over wins). Returns a new dict."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _config_from_dict(d: dict) -> Config:
    """Build a validated :class:`Config` from a fully-merged nested dict."""
    reg = build_column_registry()
    view = d.get("view", {}) or {}
    cluster = d.get("cluster", {}) or {}
    th = d.get("thresholds", {}) or {}
    panels = d.get("panels", {}) or {}
    filters = d.get("filters", {}) or {}
    probes = d.get("probes", {}) or {}

    # sort_key supersedes the legacy sort_mode. The built-in defaults layer
    # always carries sort_key=priority, so a *legacy* user file that only set
    # sort_mode would otherwise be shadowed. Resolution: take sort_key, but if
    # it is still the default 'priority' while a non-default sort_mode is set,
    # honour the legacy sort_mode. Both are kept in sync downstream.
    sort_key = str(view.get("sort_key", "priority"))
    legacy_mode = str(view.get("sort_mode", "priority"))
    if sort_key == "priority" and legacy_mode != "priority":
        sort_key = legacy_mode
    if sort_key not in SORTABLE_KEYS:
        sort_key = "priority"
    # legacy sort_mode mirrors sort_key when the latter is one of its values,
    # else falls back to priority (so an exotic sort_key never breaks old code).
    sort_mode = sort_key if sort_key in _VALID_SORT else "priority"
    sort_desc = _coerce_bool(view.get("sort_desc"), False)

    accent = str(view.get("theme_accent", "cyan"))
    if accent not in _VALID_ACCENTS:
        accent = "cyan"
    summary_style = str(view.get("summary_style", "tiles"))
    if summary_style not in _VALID_SUMMARY_STYLES:
        summary_style = "tiles"
    group_by_node = _coerce_bool(view.get("group_by_node"), False)
    name_width = clamp_name_width(view.get("name_width", NAME_WIDTH_DEFAULT))

    try:
        interval = max(1.0, float(view.get("interval", 3.0)))
    except (TypeError, ValueError):
        interval = 3.0

    cols_in = d.get("columns", []) or []
    columns = [c for c in cols_in if c in reg]
    if not columns:
        columns = [k for k in _DEFAULT_COLUMN_ORDER if reg[k].default_visible]

    ns = cluster.get("namespaces", []) or []
    if isinstance(ns, str):
        ns = [n.strip() for n in ns.split(",") if n.strip()]

    # health_probes: normalise to a list of plain dicts {name, url, fields}.
    health_probes: list = []
    for p in probes.get("health_probes", []) or []:
        if isinstance(p, dict) and p.get("name") and p.get("url"):
            health_probes.append({
                "name": str(p["name"]),
                "url": str(p["url"]),
                "fields": dict(p.get("fields", {}) or {}),
            })

    def _int(src, key, default):
        try:
            return int(src.get(key, default))
        except (TypeError, ValueError):
            return default

    return Config(
        interval=interval,
        timezone=str(view.get("timezone", "")),
        sort_mode=sort_mode,
        sort_key=sort_key,
        sort_desc=sort_desc,
        theme_accent=accent,
        summary_style=summary_style,
        group_by_node=group_by_node,
        name_width=name_width,
        name_filter=str(filters.get("name_filter", "")),
        hide_completed=_coerce_bool(filters.get("hide_completed"), True),
        only_problems=_coerce_bool(filters.get("only_problems"), False),
        namespaces=list(ns) or ["default"],
        context=str(cluster.get("context", "")),
        cpu_warn=_int(th, "cpu_warn", 75), cpu_crit=_int(th, "cpu_crit", 90),
        mem_warn=_int(th, "mem_warn", 80), mem_crit=_int(th, "mem_crit", 92),
        pvc_warn=_int(th, "pvc_warn", 75), pvc_crit=_int(th, "pvc_crit", 90),
        show_summary=_coerce_bool(panels.get("summary"), True),
        show_trends=_coerce_bool(panels.get("trends"), True),
        show_podtable=_coerce_bool(panels.get("podtable"), True),
        show_events=_coerce_bool(panels.get("events"), True),
        show_pvc=_coerce_bool(panels.get("pvc"), False),
        show_alerts=_coerce_bool(panels.get("alerts"), True),
        show_health=_coerce_bool(panels.get("health"), True),
        alertmanager_url=str(probes.get("alertmanager_url", "") or ""),
        health_probes=health_probes,
        columns=columns,
        profile_name=str(d.get("profile", "generic")),
    )


def _profile_layer(profile: Optional[Profile]) -> dict:
    """Express a Profile as a config-dict layer (layer 2)."""
    if profile is None:
        return {}
    layer: dict = {
        "view": {"timezone": profile.timezone},
        "cluster": {"namespaces": list(profile.namespaces)} if profile.namespaces else {},
        "thresholds": {
            "cpu_warn": profile.cpu_warn, "cpu_crit": profile.cpu_crit,
            "mem_warn": profile.mem_warn, "mem_crit": profile.mem_crit,
            "pvc_warn": profile.pvc_warn, "pvc_crit": profile.pvc_crit,
        },
        "profile": profile.name,
    }
    # cluster-linked probes: pass the profile's alertmanager URL + health probes
    # into the config layer so the unified Config carries them (the user file may
    # still override). Empty values are dropped so they never clobber a user one.
    probes: dict = {}
    if profile.alertmanager_url:
        probes["alertmanager_url"] = profile.alertmanager_url
    if profile.health_probes:
        probes["health_probes"] = [
            {"name": hp.name, "url": hp.url, "fields": dict(hp.fields or {})}
            for hp in profile.health_probes
        ]
    if probes:
        layer["probes"] = probes
    # drop empty timezone so it doesn't clobber a user setting with ""
    if not profile.timezone:
        layer["view"].pop("timezone", None)
        if not layer["view"]:
            layer.pop("view", None)
    if not layer.get("cluster"):
        layer.pop("cluster", None)
    return layer


def _read_user_file(path: str) -> dict:
    """Read a user config YAML/JSON file. Returns {} on any problem."""
    if not path or not os.path.exists(path):
        if path == CONFIG_PATH:
            # One-time rename migration: pre-rename kubetop/ktop configs are
            # adopted so existing users keep their settings (highest priority).
            migrated = _migrate_legacy_config()
            if migrated:
                return migrated
            # else fall back to absorbing the legacy state.json sidebar choices
            return _migrate_legacy_state()
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if _HAS_YAML:
            data = yaml.safe_load(text) or {}
        else:
            import json
            data = json.loads(text or "{}")
        if not isinstance(data, dict):
            return {}
        # An empty alertmanager_url / health_probes in the user file is the
        # auto-persisted default ("not set by the user"), not an intentional
        # override — drop it so it doesn't clobber a value the profile provides.
        # A non-empty user value still wins (it survives this strip).
        probes = data.get("probes")
        if isinstance(probes, dict):
            if not probes.get("alertmanager_url"):
                probes.pop("alertmanager_url", None)
            if not probes.get("health_probes"):
                probes.pop("health_probes", None)
            if not probes:
                data.pop("probes", None)
        return data
    except Exception:
        return {}


def _migrate_legacy_config() -> dict:
    """Adopt a pre-rename config file into the new kutop location.

    The project was renamed ktop -> kubetop -> kutop. When the new
    ``~/.config/kutop/config.yaml`` does not yet exist but an old config does, we
    read it and copy it across so existing users keep every setting.
    Returns the parsed config dict (so it layers exactly like a user file would)
    or ``{}`` when there is nothing to migrate. Never raises.
    """
    if os.path.exists(CONFIG_PATH):
        return {}
    source_path = ""
    for candidate in (_LEGACY_KUBETOP_CONFIG_PATH, _LEGACY_KTOP_CONFIG_PATH):
        if os.path.exists(candidate):
            source_path = candidate
            break
    if not source_path:
        return {}
    try:
        with open(source_path, encoding="utf-8") as fh:
            text = fh.read()
        if _HAS_YAML:
            data = yaml.safe_load(text) or {}
        else:
            import json
            data = json.loads(text or "{}")
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    # Best-effort copy into the new location so the migration is sticky and the
    # old file is left untouched. Failure here is non-fatal (the loaded dict is
    # still returned, just not yet persisted under the new name).
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    return data


def _migrate_legacy_state() -> dict:
    """Absorb a legacy ``state.json`` (kutop/kubetop/ktop) into a layer.

    Returns a partial config dict (only the keys the old state knew about) so
    the user's previous sidebar choices survive the upgrade. Checks the current
    config dir first, then pre-rename dirs. Never raises.
    """
    st = None
    for candidate in (_STATE_PATH, _LEGACY_KUBETOP_STATE_PATH, _LEGACY_KTOP_STATE_PATH):
        try:
            import json
            with open(candidate, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                st = loaded
                break
        except Exception:
            continue
    if st is None:
        return {}
    layer: dict = {"view": {}, "cluster": {}, "panels": {}}
    if "sort_mode" in st:
        layer["view"]["sort_mode"] = st["sort_mode"]
    if "namespaces" in st and st["namespaces"]:
        layer["cluster"]["namespaces"] = list(st["namespaces"])
    if "show_events" in st:
        layer["panels"]["events"] = bool(st["show_events"])
    if "show_pvc" in st:
        layer["panels"]["pvc"] = bool(st["show_pvc"])
    # prune empties
    return {k: v for k, v in layer.items() if v}


def load_config(
    profile: Optional[Profile] = None,
    user_path: Optional[str] = None,
    cli_overrides: Optional[dict] = None,
    base_overrides: Optional[dict] = None,
) -> Config:
    """Layer defaults -> profile -> base -> user file -> CLI flag overrides.

    ``base_overrides`` are seed defaults applied BELOW the saved user file —
    used for positional args (namespaces / interval) so e.g. ``make top``'s
    ``TOP_NS`` seeds the first run but the user's saved choices win afterwards.
    ``cli_overrides`` are explicit flags applied ABOVE the user file (a flag the
    user typed should win). Both are nested config dicts shaped like the file.
    """
    merged = _default_config_dict()
    merged = _deep_merge(merged, _profile_layer(profile))
    if base_overrides:
        merged = _deep_merge(merged, base_overrides)
    path = user_path or CONFIG_PATH
    merged = _deep_merge(merged, _read_user_file(path))
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)
    return _config_from_dict(merged)


def save_config(cfg: Config, path: Optional[str] = None) -> str:
    """Persist a Config to ``~/.config/kutop/config.yaml`` (or ``path``).

    Returns the path written. Raises if PyYAML is unavailable (caller decides
    how to surface that). Creates the directory tree as needed.
    """
    target = path or CONFIG_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    text = dump_config_yaml(cfg)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    # Migration complete: the legacy state.json has been absorbed into config.yaml.
    if target == CONFIG_PATH:
        for old_state in (_STATE_PATH, _LEGACY_KUBETOP_STATE_PATH, _LEGACY_KTOP_STATE_PATH):
            if os.path.exists(old_state):
                try:
                    os.remove(old_state)
                except Exception:
                    pass
    return target


# ─────────────────────── annotated YAML skeleton dump ───────────────────────


def dump_config_yaml(cfg: Optional[Config] = None) -> str:
    """Render the COMPLETE config skeleton as annotated YAML.

    Every option appears with its current (or default) value and a short inline
    comment. Hand-editable; also exactly what ``kutop --dump-config`` prints and
    what :func:`save_config` writes. Does NOT require PyYAML (we emit text).
    """
    if cfg is None:
        cfg = _config_from_dict(_default_config_dict())
    reg = build_column_registry()

    def b(v: bool) -> str:
        return "true" if v else "false"

    lines: list = []
    lines.append("# kutop configuration skeleton — every option, with defaults.")
    lines.append("# Location: ~/.config/kutop/config.yaml  (edit by hand or via the")
    lines.append("# Options modal, key 'o', in the running app). Layering order:")
    lines.append("#   built-in defaults -> --profile -> this file -> CLI flags.")
    lines.append(f"profile: {cfg.profile_name}        # active profile name (read-only)")
    lines.append("")
    lines.append("view:")
    lines.append(f"  interval: {cfg.interval}            # refresh seconds (min 1.0)")
    tz = cfg.timezone or '""'
    lines.append(f"  timezone: {tz}          # IANA tz for timestamps; \"\" = host local")
    lines.append(f"  sort_key: {cfg.sort_key}      # sort column: {' | '.join(SORTABLE_KEYS)}")
    lines.append(f"  sort_desc: {b(cfg.sort_desc)}        # reverse sort direction (▼)")
    lines.append(f"  theme_accent: {cfg.theme_accent}    # {' | '.join(_VALID_ACCENTS)}")
    lines.append(f"  summary_style: {cfg.summary_style}    # {' | '.join(_VALID_SUMMARY_STYLES)} (top header layout)")
    lines.append(f"  group_by_node: {b(cfg.group_by_node)}   # group pods under their node header rows")
    lines.append(f"  name_width: {cfg.name_width}           # NODE/POD column width in cells (drag its right edge to resize; {NAME_WIDTH_MIN}..{NAME_WIDTH_MAX})")
    lines.append("")
    lines.append("cluster:")
    ns = ", ".join(cfg.namespaces) if cfg.namespaces else ""
    lines.append(f"  namespaces: [{ns}]   # namespaces to watch (CSV / list)")
    ctx = cfg.context or '""'
    lines.append(f"  context: {ctx}             # kubeconfig context; \"\" = current")
    lines.append("")
    lines.append("filters:                  # adjust which pods the table shows")
    nf = cfg.name_filter or '""'
    lines.append(f"  name_filter: {nf}        # case-insensitive substring on pod name")
    lines.append(f"  hide_completed: {b(cfg.hide_completed)}   # drop Succeeded/Completed pods")
    lines.append(f"  only_problems: {b(cfg.only_problems)}    # only non-Running / restarts>0 / oom")
    lines.append("")
    lines.append("thresholds:               # OK/Warn/Crit coloring (percent)")
    lines.append(f"  cpu_warn: {cfg.cpu_warn}            # CPU warn %")
    lines.append(f"  cpu_crit: {cfg.cpu_crit}            # CPU crit %")
    lines.append(f"  mem_warn: {cfg.mem_warn}            # MEM warn %")
    lines.append(f"  mem_crit: {cfg.mem_crit}            # MEM crit %")
    lines.append(f"  pvc_warn: {cfg.pvc_warn}            # PVC warn %")
    lines.append(f"  pvc_crit: {cfg.pvc_crit}            # PVC crit %")
    lines.append("")
    lines.append("panels:                   # show/hide each dashboard panel")
    lines.append(f"  summary: {b(cfg.show_summary)}         # top aggregate counter bar")
    lines.append(f"  trends: {b(cfg.show_trends)}          # CPU/MEM trend sparklines")
    lines.append(f"  podtable: {b(cfg.show_podtable)}        # main node/pod table")
    lines.append(f"  events: {b(cfg.show_events)}          # warning events panel")
    lines.append(f"  pvc: {b(cfg.show_pvc)}             # cluster-wide PVC list panel (off by default; storage is per-pod)")
    lines.append(f"  alerts: {b(cfg.show_alerts)}          # AlertManager alerts panel (needs probes.alertmanager_url)")
    lines.append(f"  health: {b(cfg.show_health)}          # workload health row (needs probes.health_probes)")
    lines.append("")
    lines.append("probes:                   # cluster-linked HTTP probes (opt-in; stdlib urllib)")
    am = cfg.alertmanager_url or '""'
    lines.append(f"  alertmanager_url: {am}   # AlertManager /api/v2/alerts URL; \"\" = alerts panel hidden")
    if cfg.health_probes:
        lines.append("  health_probes:          # each scrapes a URL, extracts fields via regex (group 1)")
        for hp in cfg.health_probes:
            lines.append(f"    - name: {hp.get('name', '')}")
            lines.append(f"      url: {hp.get('url', '')}")
            flds = hp.get("fields", {}) or {}
            if flds:
                lines.append("      fields:")
                for label, pat in flds.items():
                    lines.append(f"        {label}: '{pat}'")
            else:
                lines.append("      fields: {}")
    else:
        lines.append("  health_probes: []       # list of {name, url, fields:{label: regex}}; [] = health row hidden")
    lines.append("")
    lines.append("# columns: ordered list of VISIBLE table columns. Reorder/remove")
    lines.append("# to customise; available keys (default-visible marked '*'):")
    for key in _DEFAULT_COLUMN_ORDER:
        spec = reg[key]
        star = "*" if spec.default_visible else " "
        lines.append(f"#   {star} {key:<12} {spec.help}")
    lines.append("columns:")
    for key in cfg.visible_columns():
        lines.append(f"  - {key}")
    lines.append("")
    return "\n".join(lines) + "\n"
