"""The kutop Textual application.

Composes a modern dashboard:
  * SummaryBar (aggregate counters)
  * two TrendGraph meters (CPU / MEM overall) — fed by rolling history
  * main Node/Pod DataTable with per-pod usage-vs-limit gauges + status
    highlighting (OOMKilled / CrashLoopBackOff / Pending)
  * Events panel + PVC panel (with kubelet-sourced usage)
  * collapsible control sidebar (ns switch / sort / panel toggles)

Data acquisition runs in a background thread worker (``@work(thread=True)``);
the UI thread never blocks on subprocess. Ordering/timezone/thresholds come
only from the Profile — there is NO hardcoded workload knowledge in this module.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import subprocess
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    Input,
    Static,
)

from .. import __version__, model
from ..config import (
    Config,
    METRICS_RESOLUTION_SECS,
    Profile,
    REFRESH_INTERVAL_SECS,
    SORTABLE_KEYS,
    SORT_KEY_TO_COLUMN,
    COLUMN_TO_SORT_KEY,
    build_column_registry,
    list_profiles,
    load_config,
    load_profile,
    save_config,
)
from ..fetch import Fetcher
from ..model import Pod, Snapshot, fmt_age, age_seconds
from .header import MetricsIndicator, ThemeHeader, ThemeHeaderIcon
from .modals import DescribeModal, EventDetailModal, LogViewerModal, YamlViewModal
from .sidebar import SidebarPanel, SidebarState
from .table import ResizableDataTable
from .widgets import (
    _severity_style,
    OptionsModal,
    SearchBar,
    SummaryBar,
    TrendGraph,
    ConfirmModal,
    bar_gauge,
    level_color,
)

__all__ = [
    "TopApp", "RenderCtx",
    # re-exported for backward compatibility after the module split
    "ThemeHeaderIcon", "MetricsIndicator", "ThemeHeader",
    "LogViewerModal", "DescribeModal", "EventDetailModal",
    "SidebarState", "SidebarPanel", "ResizableDataTable",
    "REFRESH_INTERVAL_SECS",
]

_HISTORY = 120
_HIDDEN_THEMES = {"ansi-dark", "ansi-light"}


_BINDING_SPECS = [
    ("q", "quit_hint", "Quit?"),
    ("r", "refresh", "Refresh"),
    ("o", "open_options", "Options"),
    ("slash", "search", "Search"),
    ("escape", "clear_search", "Clear"),
    # NOTE: no "tab" binding — Screen's built-in tab->focus_next has priority,
    # so a tab binding here would be dead weight that only misleads the footer.
    ("b", "toggle_sidebar", "Sidebar"),
    ("s", "cycle_sort", "Sort"),
    ("S", "toggle_sort_dir", "SortDir"),
    ("g", "toggle_group", "Group"),
    ("l", "show_logs", "Logs"),
    ("d", "describe_pod", "Describe"),
    ("y", "show_yaml", "YAML"),
    ("t", "shell_pod", "Shell"),
    ("x", "delete_pod", "Delete"),
    ("X", "restart_pod", "Restart"),
    ("e", "toggle_events", "Events"),
    ("v", "toggle_pvc", "PVC"),
    ("a", "toggle_alerts", "Alerts"),
    ("h", "toggle_health", "Health"),
    ("R", "reload_config", "Reload"),
]


def _binding_key(action: str) -> str:
    """Return the displayed key for an app action from the binding SOT."""
    for key, bound_action, _desc in _BINDING_SPECS:
        if bound_action == action:
            return _key_label(key)
    return ""


def _key_label(key: str) -> str:
    return {"slash": "/", "escape": "esc"}.get(key, key)


# ── render context for column accessors ───────────────────────────────────────


class RenderCtx:
    """Helpers passed to column accessors so they stay model/rich-light.

    Carries threshold lookups, the bar-gauge builder, and name-cell builders so
    the column registry can render Pod/Node cells without re-implementing the
    highlight/gauge logic that lives in the app + widgets.
    """

    def __init__(self, app: "TopApp") -> None:
        self._app = app

    def color(self, value, kind: str) -> str:
        warn, crit = self._app.cfg.threshold(kind)
        return level_color(value, warn, crit)

    def gauge(self, value, kind: str):
        warn, crit = self._app.cfg.threshold(kind)
        return bar_gauge(value, warn, crit)

    def pod_name_cell(self, pod: Pod) -> Text:
        return self._app._pod_name_cell(pod)

    def node_name_cell(self, node) -> Text:
        # In k8s the nodegroup (node.role: managed node group / pool) matters more
        # than the node hostname, so lead with it and show the short node name
        # secondary/dim. Any provider domain suffix is dropped.
        marker = "◆" if node.ready else "✖"
        mstyle = "bold cyan" if node.ready else "bold red"
        cell = Text(f"{marker} ")
        cell.append(node.role or "node", style=mstyle)
        short = (node.name or "").split(".", 1)[0]
        if short:
            cell.append(f"  {short}", style="dim")
        return _fit_cell(cell, self._app.cfg.name_width)

    def age_cell(self, pod: Pod):
        # human age ("3d"/"5h"/"12m") from the pod's start_time; "-" when unknown
        secs = pod.age_secs
        if secs is None:
            return Text("-", style="dim")
        return Text(model.fmt_age(secs), style="")


# The NODE/POD name cell is fit to the COLUMN WIDTH (Config.name_width, which the
# user can drag-resize), NOT a fixed character count. The fully-styled cell
# (glyph + name + bracketed [ns]/(ready) annotations) is truncated with a single
# trailing "…" only when its rendered cell-length exceeds the column width — so a
# wider column reveals more of the name and a narrow one ellipsises. Styling is
# preserved across the truncation because we slice the Rich ``Text`` itself.


def _fit_cell(text: Text, width: int) -> Text:
    """Fit a styled ``Text`` cell to ``width`` cells, ellipsising on overflow.

    Returns ``text`` unchanged when it already fits. Otherwise slices the Rich
    Text (preserving per-span styling) to ``width - 1`` cells and appends a dim
    "…" so the total rendered length is exactly ``width``. Width is the COLUMN
    width, so the same name shows fuller in a wider column and shorter in a
    narrow one. Defensive: a non-positive width yields an empty cell.
    """
    if width <= 0:
        return Text("")
    if text.cell_len <= width:
        return text
    if width == 1:
        return Text("…", style="dim")
    # Rich Text.truncate trims to the cell width, accounting for double-width
    # glyphs; we reserve one cell for the ellipsis and append it ourselves so
    # the marker keeps its own dim style instead of inheriting the last span's.
    head = text.copy()
    head.truncate(width - 1)
    head.append("…", style="dim")
    return head


# ── timezone resolution ───────────────────────────────────────────────────────


def _resolve_tz(tz_name: str):
    """Return a tzinfo for the given IANA name, or None for host local tz."""
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        return None


def _fmt_event_ts(ts_utc: str, tz) -> str:
    """Format an ISO/UTC event timestamp as HH:MM:SS in the target tz."""
    if not ts_utc:
        return "-"
    try:
        s = ts_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(tz) if tz else dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts_utc[11:19] if len(ts_utc) >= 19 else ts_utc


def _status_glyph(pod: Pod) -> tuple[str, str]:
    """(glyph, style) reflecting pod health for the row marker."""
    if pod.oomkilled:
        return "●", "bold red"
    if pod.crashloop:
        return "●", "bold red"
    if pod.phase == "Failed":
        return "●", "bold red"
    if pod.phase in ("Succeeded",):
        return "○", "dim"
    if pod.phase == "Pending":
        return "◐", "bold yellow"
    if pod.phase == "Running":
        parts = pod.ready.split("/")
        if len(parts) == 2 and parts[0] == parts[1] and parts[0] != "0":
            return "●", "bold green"
        return "◐", "bold yellow"
    return "●", "dim"


# Modals live in .modals, the sidebar in .sidebar, the resizable table in
# .table, and the header widgets in .header — re-exported above for
# backward-compatible imports.


# ── main app ───────────────────────────────────────────────────────────────────


class TopApp(App):
    CSS_PATH = os.path.join(os.path.dirname(__file__), "theme.tcss")
    TITLE = f"kutop v{__version__}"

    BINDINGS = [
        *_BINDING_SPECS,
        # Enter confirms a pending quit. priority=True beats the focused
        # widget, but check_action() disables the binding whenever no quit is
        # pending, so Enter falls through to tables/inputs/modals unchanged in
        # normal use.
        Binding("enter", "confirm_quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        namespaces: list[str],
        interval: float = REFRESH_INTERVAL_SECS,  # deprecated: cadence is fixed
        profile: Optional[Profile] = None,
        config: Optional[Config] = None,
        context: Optional[str] = None,
        allow_destructive: bool = False,
        log_tail: int = 150,
        interval_deprecated: bool = False,
        discover_namespaces: bool = True,
        auto_refresh: bool = True,
        force_color: bool = False,
        config_path: Optional[str] = None,
    ) -> None:
        no_color = os.environ.pop("NO_COLOR", None) if force_color else None
        try:
            super().__init__()
        finally:
            if no_color is not None:
                os.environ["NO_COLOR"] = no_color
        self.profile = profile or Profile()
        # Unified config: the single source of truth for everything the user can
        # customise. The CLI builds it (defaults->profile->file->CLI). If a caller
        # omits it (legacy callers / snapshot harness), synthesise one from the
        # given namespaces/interval/profile so behaviour is unchanged.
        if config is None:
            config = Config(
                timezone=self.profile.timezone,
                namespaces=list(namespaces) or list(self.profile.namespaces) or ["default"],
                context=context or "",
                cpu_warn=self.profile.cpu_warn, cpu_crit=self.profile.cpu_crit,
                mem_warn=self.profile.mem_warn, mem_crit=self.profile.mem_crit,
                pvc_warn=self.profile.pvc_warn, pvc_crit=self.profile.pvc_crit,
                profile_name=self.profile.name,
                alertmanager_url=self.profile.alertmanager_url,
                health_probes=[
                    {"name": hp.name, "url": hp.url, "fields": dict(hp.fields)}
                    for hp in self.profile.health_probes
                ],
            )
            config.columns = config.visible_columns()
        self.cfg = config
        requested_theme = str(self.cfg.theme or "textual-dark")
        self.theme = self._coerce_theme(requested_theme)
        if self.theme != requested_theme:
            # an unknown --theme/file theme must launch (fall back) but not
            # silently: on_mount surfaces every load warning as a toast
            self.cfg.load_warnings.append(
                f"theme {requested_theme!r} is not available; using {self.theme}"
            )
        self.cfg.theme = self.theme

        self.namespaces = list(self.cfg.namespaces)
        # Cadence is fixed; ignore any legacy interval from config/CLI callers.
        self.interval = REFRESH_INTERVAL_SECS
        self.cfg.interval = REFRESH_INTERVAL_SECS
        self.context = self.cfg.context or None
        # actual kube context name for display, resolved from kubectl on mount
        self._resolved_context = ""
        self.allow_destructive = allow_destructive
        self.log_tail = log_tail
        # CLI saw the deprecated positional interval: its stderr note is hidden
        # by the fullscreen TUI, so surface it once as a toast after mount.
        self._interval_deprecated = interval_deprecated
        self.tz = _resolve_tz(self.cfg.timezone)
        self.render_ctx = RenderCtx(self)
        self.column_registry = build_column_registry()

        self.fetcher = Fetcher(
            self.namespaces,
            context=self.context,
            alertmanager_url=self.cfg.alertmanager_url,
            health_probes=self._probe_specs(self.cfg),
        )
        # remember the path the config was loaded from for hot-reload (R)
        self._config_path = config_path
        self.snapshot: Snapshot = Snapshot()
        self.cpu_hist: deque[int] = deque(maxlen=_HISTORY)
        self.mem_hist: deque[int] = deque(maxlen=_HISTORY)

        # Panel/sort state lives ONLY on self.cfg — the show_*/sort_mode/
        # namespaces attributes below are properties delegating to it, so the
        # old hand-synced mirror copies (and their drift bugs) are gone.
        # Live search term (key '/'). Config.name_filter is only an initial
        # CLI --filter seed; it is cleared before any save so ad-hoc searches
        # cannot survive a relaunch.
        self._search_term = (self.cfg.name_filter or "").strip()
        self.cfg.name_filter = ""
        # Compiled name-filter matcher, memoized on the term string so the
        # regex is not recompiled on every 5s render: (term, matcher).
        self._filter_cache: "tuple[str, object, bool]" = ("", None, False)
        # Namespaces discovered live on the cluster (for the Options multi-select).
        self._discovered_ns: list[str] = []
        self._discovered_contexts: list[str] = []
        # Selectable profile names for the sidebar dropdown (discovered once;
        # profiles don't change on disk mid-session).
        self._profile_opts = self._profile_options_list()
        # guarded off for --self-test so the headless smoke test never shells out
        self._discover_namespaces = discover_namespaces
        self._auto_refresh = auto_refresh
        self._refresh_timer = None
        self._loaded = False
        self._quit_hint_deadline = 0.0
        # last startup-failure text, so column rebuilds while unloaded can
        # restore the guidance rows instead of leaving the table blank
        self._startup_error = ""
        # anchors for fire-and-forget runners: the loop holds only weak refs
        self._bg_tasks: "set[asyncio.Task]" = set()
        # Fetch lifecycle: _fetching gates one worker at a time; _fetch_gen is a
        # scope token bumped on every namespace/context/profile change so a
        # snapshot fetched under the OLD scope is dropped instead of being
        # rendered as the NEW one; _refresh_pending queues an urgent refresh
        # that arrived while a fetch was in flight.
        self._fetching = False
        self._fetch_gen = 0
        self._refresh_pending = False
        self._last_refresh_error = ""
        # First-run orientation toast: shown at most once per session, when the
        # FIRST successful snapshot applies on an out-of-the-box generic config.
        self._shown_first_run_hint = False

    # ── cfg is the single source of truth ─────────────────────────────────────
    # These properties keep the historical attribute API (sidebar handlers,
    # toggle actions, and tests assign app.show_events etc.) while storing the
    # value only on self.cfg — there is no copy to fall out of sync.

    @property
    def namespaces(self) -> "list[str]":
        return list(self.cfg.namespaces)

    @namespaces.setter
    def namespaces(self, value: "list[str]") -> None:
        self.cfg.namespaces = [str(n) for n in (value or [])]

    @property
    def sort_mode(self) -> str:
        return self.cfg.sort_mode

    @sort_mode.setter
    def sort_mode(self, value: str) -> None:
        self.cfg.sort_mode = value

    @property
    def show_events(self) -> bool:
        return self.cfg.show_events

    @show_events.setter
    def show_events(self, value: bool) -> None:
        self.cfg.show_events = bool(value)

    @property
    def show_pvc(self) -> bool:
        return self.cfg.show_pvc

    @show_pvc.setter
    def show_pvc(self, value: bool) -> None:
        self.cfg.show_pvc = bool(value)

    @property
    def show_alerts(self) -> bool:
        return self.cfg.show_alerts

    @show_alerts.setter
    def show_alerts(self, value: bool) -> None:
        self.cfg.show_alerts = bool(value)

    @property
    def show_health(self) -> bool:
        return self.cfg.show_health

    @show_health.setter
    def show_health(self, value: bool) -> None:
        self.cfg.show_health = bool(value)

    def notify(self, message, *, title="", severity="information",
               timeout=None, markup=False) -> None:
        """Show a toast as PLAIN TEXT by default (markup=False).

        Every kutop toast carries dynamic content — kubectl stderr, a failing
        command line, namespaces, paths, profile names — none of it intended as
        Textual markup. A kubectl timeout in particular embeds the full argv
        (``['kubectl', ...]``); parsed as markup the unbalanced ``[`` raises
        MarkupError from inside the compositor and crashes the whole app during
        layout. Defaulting markup off makes notifications immune to their own
        content; a caller that genuinely wants markup passes ``markup=True``.
        """
        super().notify(message, title=title, severity=severity,
                       timeout=timeout, markup=markup)

    def _all_plugins(self) -> list:
        """Every registered plugin (for mounting/visibility/render).

        Panels are mounted for all plugins so a toggled-on-but-unconfigured one
        (e.g. health with no probes) can show a setup hint instead of vanishing;
        data fetching still runs only for enabled plugins.
        """
        try:
            from ..plugins import iter_plugins
        except Exception:
            return []
        try:
            return list(iter_plugins())
        except Exception:
            return []

    @staticmethod
    def _probe_specs(cfg: Config) -> list:
        """Translate the config's health_probes dicts into HealthProbe objects.

        The Fetcher only needs ``.name/.url/.fields`` attributes; we reuse the
        :class:`~kutop.config.HealthProbe` dataclass so the probes module gets the
        attribute access it expects. Empty -> [] (no scraping at all).
        """
        from ..config import HealthProbe
        return [HealthProbe.from_any(p) for p in cfg.health_probes or []]

    def _coerce_theme(self, theme: str) -> str:
        """Return a valid Textual theme name, falling back to textual-dark."""
        name = str(theme or "textual-dark")
        if name in self.available_themes and name not in _HIDDEN_THEMES:
            return name
        return "textual-dark"

    def _theme_options(self) -> list[str]:
        """Return usable Textual themes for kutop's layout."""
        return [
            name for name in self.available_themes.keys()
            if name not in _HIDDEN_THEMES
        ]

    def _context_options(self) -> list[str]:
        """Return kubeconfig context names for the Options selector.

        Discovery is skipped when live discovery is disabled so self-test and
        snapshot modes stay kubectl-free. The current config value is always
        retained, even if the kubeconfig no longer lists it.
        """
        names = list(self._discovered_contexts)
        if self._discover_namespaces and not names:
            try:
                proc = subprocess.run(
                    ["kubectl", "config", "get-contexts", "-o", "name"],
                    text=True,
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
                if proc.returncode == 0:
                    names = [
                        line.strip()
                        for line in proc.stdout.splitlines()
                        if line.strip()
                    ]
                    self._discovered_contexts = list(names)
            except Exception:
                names = []
        if self.cfg.context and self.cfg.context not in names:
            names.insert(0, self.cfg.context)
        return names

    def _cached_context_options(self) -> list[str]:
        """Context names from the discovery cache only — never shells kubectl.

        Used on the UI thread (opening the Options modal) so it can never block
        on a subprocess; the cache is filled by the mount-time discovery worker.
        """
        names = list(self._discovered_contexts)
        if self.cfg.context and self.cfg.context not in names:
            names.insert(0, self.cfg.context)
        return names

    def _sidebar_context_options(self) -> list[str]:
        """Cheap context list for the sidebar Select — cache only, no kubectl.

        Uses contexts already discovered (filled by the discover worker) plus the
        active one, so compose stays instant; rebuild_contexts refreshes the list
        once discovery completes.
        """
        names = list(self._discovered_contexts)
        cur = (self._display_context() or "").strip()
        if cur and cur not in names:
            names.insert(0, cur)
        return names

    # ── compose ──────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield ThemeHeader(show_clock=True, icon="☰")
        yield SummaryBar(id="summary_bar")            # CRITERIA #5: SummaryBar composed
        with Horizontal(id="trends"):
            yield TrendGraph("CPU OVERALL", "cpu", id="cpu_trend")
            yield TrendGraph("MEM OVERALL", "mem", id="mem_trend")
        with Horizontal(id="main_horizontal"):
            yield SidebarPanel(
                self._sidebar_ns_options(),
                self._sidebar_state(),
                profile_options=self._profile_opts,
                context_options=self._sidebar_context_options(),
                id="sidebar",
            )
            with Vertical(id="main_view"):
                yield SearchBar(id="search_bar", classes="-hidden")
                # The top row holds the generic alerts panel plus any enabled
                # optional plugin panels (e.g. the health plugin). The plugins
                # are discovered through the generic seam — the core mounts
                # whatever panels they supply without importing them by name.
                with Horizontal(id="top_panels"):
                    yield DataTable(id="alerts_panel", classes="kpanel -hidden")
                    for plugin in self._all_plugins():
                        try:
                            yield plugin.make_panel()
                        except Exception:
                            continue
                # All five panels carry .kpanel for uniform chrome (border style,
                # left accent title, padding, scrollbar); each keeps its own
                # semantic border COLOR via its #id rule in theme.tcss.
                yield ResizableDataTable(id="main_table", classes="kpanel")
                with Horizontal(id="bottom_box"):
                    yield DataTable(id="events_table", classes="kpanel")
                    yield DataTable(id="pvc_table", classes="kpanel")
        yield Footer()

    def _sidebar_ns_options(self, discovered: Optional[list[str]] = None) -> list[str]:
        """Build the ordered, de-duplicated sidebar NAMESPACE list (one per row).

        The list powers a checkbox per namespace; the user ticks any combination
        to view their pods COMBINED. Order: the currently-selected / profile
        namespaces first (so the ticked defaults sit at the top), then every
        other namespace discovered live on the cluster. ``discovered`` is the
        live ``kubectl get ns`` result; when omitted (first synchronous compose,
        or discovery failed) we fall back to just the selected namespaces so
        nothing crashes and --self-test stays kubectl-free.
        """
        opts: list[str] = []
        # selected / profile default namespaces first (these start ticked)
        for ns in self.namespaces:
            if ns and ns not in opts:
                opts.append(ns)
        # then every other cluster namespace (live discovery)
        for ns in discovered or []:
            if ns and ns not in opts:
                opts.append(ns)
        return opts

    def on_mount(self) -> None:
        if self._auto_refresh:
            # resolve the real kube context name once so the sidebar shows it
            # instead of a generic "current" (skipped in --self-test)
            self._resolved_context = self.fetcher.current_context_name()
        self.apply_theme_chrome()
        self.query_one("#summary_bar", SummaryBar).set_style_mode(self.cfg.summary_style)

        mt = self.query_one("#main_table", DataTable)
        mt.cursor_type = "row"
        self._build_main_columns(mt)

        et = self.query_one("#events_table", DataTable)
        et.cursor_type = "row"
        et.add_columns("TIME", "OBJECT", "REASON", "COUNT")
        pt = self.query_one("#pvc_table", DataTable)
        pt.cursor_type = "row"
        pt.add_columns("PVC", "STORAGE (USE/CAP)", "GAUGE", "CLASS")

        at = self.query_one("#alerts_panel", DataTable)
        at.cursor_type = "row"
        at.add_columns("ALERT", "SEV", "TARGET", "SINCE")

        # Panel headers rendered ON the top border line (border_title) so every
        # panel is labelled consistently.
        mt.border_title = "PODS"
        et.border_title = "EVENTS"
        pt.border_title = "PVC"
        at.border_title = "ALERTS"

        ncols = len(self.cfg.visible_columns())
        mt.add_row(Text("Loading cluster snapshot...", style="bold yellow"),
                   *(["-"] * (ncols - 1)), key="loading")

        self.apply_panel_visibility()
        self._update_metrics_indicator()
        if self._auto_refresh:
            self.refresh_snapshot()
            self._refresh_timer = self.set_interval(self.interval, self.refresh_snapshot)

        # BUG FIX #4: discover cluster namespaces live and repopulate the
        # sidebar list. Guarded off for --self-test so no kubectl is shelled out.
        if self._discover_namespaces:
            self.run_worker(
                self._discover_ns_worker, thread=True, exclusive=False, group="ns"
            )

        if self._interval_deprecated:
            self._interval_deprecated = False
            self.notify(
                "the positional interval argument is deprecated; refresh is fixed at 5s",
                severity="warning",
                timeout=6,
            )

        # Surface config-load problems (unparseable user file, unknown theme)
        # as toasts — a silent fallback to defaults looks like lost preferences.
        for warning in self.cfg.load_warnings:
            self.notify(warning, severity="warning", timeout=8)

    def _build_main_columns(self, mt: DataTable) -> None:
        """(Re)build the main table columns from the config's visible-column list.

        Removes existing columns explicitly (some Textual versions ignore
        ``clear(columns=True)`` for already-added columns) so a re-render after a
        column toggle/reorder never mismatches cell count vs column count.
        """
        for col_key in list(mt.columns.keys()):
            try:
                mt.remove_column(col_key)
            except Exception:
                pass
        try:
            mt.clear(columns=True)
        except Exception:
            mt.clear()
        vis = self.cfg.visible_columns()
        labels = [self._column_label(k) for k in vis]
        mt.add_columns(*labels)
        # Apply the persisted NODE/POD column width so the saved (or just-dragged)
        # width is restored on every (re)build. The name column is whichever
        # visible column carries the "name" key (normally the first).
        if "name" in vis:
            self._apply_name_width(mt, vis.index("name"))

    def _name_column_index(self, mt: DataTable) -> Optional[int]:
        """Index of the NODE/POD ('name') column in the visible set, or None."""
        vis = self.cfg.visible_columns()
        return vis.index("name") if "name" in vis else None

    def _apply_name_width(self, mt: DataTable, col_index: int) -> None:
        """Pin the name column to ``cfg.name_width`` cells (auto_width off).

        Sets the underlying :class:`textual.widgets.data_table.Column` width and
        clears ``auto_width`` so the value is honoured, then recomputes the table
        layout. Best effort: a not-yet-mounted / mismatched table no-ops.
        """
        try:
            col = mt.ordered_columns[col_index]
        except (IndexError, AttributeError):
            return
        from ..config import clamp_name_width
        col.width = clamp_name_width(self.cfg.name_width)
        col.auto_width = False
        try:
            mt._update_column_widths(set())
        except Exception:
            pass
        mt.refresh()

    def _column_label(self, col_key: str):
        """Header label, with a ▲/▼ sort indicator and — on the resizable NODE/POD
        column — a ``│`` marker at the far-right cell so users can see the draggable
        boundary they grab to resize the column."""
        base = self.column_registry[col_key].label
        active = SORT_KEY_TO_COLUMN.get(self.cfg.sort_key)
        if active == col_key:
            base = f"{base} {'▼' if self.cfg.sort_desc else '▲'}"
        if col_key == "name":
            w = max(4, int(self.cfg.name_width))
            label = Text(base[: w - 1].ljust(w - 1))
            label.append("│", style="bold yellow")  # the drag-to-resize handle
            return label
        return base

    # ── namespace discovery worker (BUG FIX #4) ────────────────────────────────
    def _discover_ns_worker(self) -> None:
        """Thread worker: list cluster namespaces, then repopulate the sidebar.

        Falls back silently to the profile namespaces on any failure (the list
        already shows them) so discovery never crashes the app.
        """
        try:
            discovered = self.fetcher.list_namespaces()
        except Exception:
            discovered = []
        try:
            self._context_options()  # fill the _discovered_contexts cache (kubectl)
        except Exception:
            pass
        # always repopulate so the CONTEXT dropdown refreshes even if the ns list
        # came back empty
        self.call_from_thread(self._populate_ns_list, discovered)

    def _populate_ns_list(self, discovered: list[str]) -> None:
        """Rebuild the sidebar NAMESPACE checkboxes + CONTEXT dropdown from live
        discovery.

        Keeps the currently-ticked namespaces ticked (and ensures every selected
        namespace stays present even if discovery omits it), and refills the
        context Select from the now-discovered kubeconfig contexts.
        """
        try:
            sidebar = self.query_one("#sidebar", SidebarPanel)
        except Exception:
            return
        if discovered:
            # remember the full cluster ns list for the Options multi-select
            self._discovered_ns = list(discovered)
            sidebar.rebuild_namespaces(
                self._sidebar_ns_options(discovered), list(self.namespaces)
            )
        sidebar.rebuild_contexts(
            self._sidebar_context_options(), self._display_context()
        )

    # ── background fetch worker ────────────────────────────────────────────────
    def refresh_snapshot(self) -> None:
        """Kick a threaded fetch; never blocks the UI thread.

        A full fetch (kubelet stats + alerts + health) can take longer than the
        refresh interval. If we let the interval restart an exclusive worker every
        tick, each in-flight fetch is cancelled before its LAST steps (alerts /
        health) complete — so those panels never populate. Skip the tick while a
        fetch is in flight: the running fetch completes and applies in full, and
        the effective cadence becomes ~fetch-duration instead of thrashing.
        Scope changes must not be lost to that skip — they go through
        :meth:`_request_refresh`, which queues the refresh to run as soon as the
        in-flight fetch returns.
        """
        if self._fetching:
            return
        self._fetching = True
        self._refresh_pending = False
        gen = self._fetch_gen
        self.run_worker(
            lambda: self._fetch_worker(gen), thread=True, exclusive=True,
            group="fetch",
        )

    def _request_refresh(self) -> None:
        """Urgent refresh (scope change / manual): now, or queued if in flight."""
        self._refresh_pending = True
        self.refresh_snapshot()

    def _bump_fetch_gen(self) -> None:
        """Invalidate in-flight fetches: their scope (ns/context/probes) is stale."""
        self._fetch_gen += 1

    def _fetch_worker(self, gen: int) -> None:
        try:
            if not self._loaded:
                snap = self.fetcher.fetch_core()
                try:
                    # first paint gets a COPY: enrich_snapshot below keeps
                    # mutating `snap` on this thread while the UI renders it
                    self.call_from_thread(
                        self._apply_snapshot, copy.deepcopy(snap), gen
                    )
                except Exception:
                    pass
                snap = self.fetcher.enrich_snapshot(snap)
            else:
                snap = self.fetcher.fetch()
            # marshal back to the UI thread; if the app is tearing down the call
            # may be rejected — swallow it so the worker thread returns cleanly.
            try:
                self.call_from_thread(self._apply_snapshot, snap, gen)
            except Exception:
                pass
        finally:
            self._fetching = False
            # a scope change or manual refresh arrived mid-fetch: serve it now
            if self._refresh_pending or gen != self._fetch_gen:
                try:
                    self.call_from_thread(self.refresh_snapshot)
                except Exception:
                    pass

    # ── shutdown ───────────────────────────────────────────────────────────────
    def action_quit_hint(self) -> None:
        """Require two consecutive q presses before quitting.

        A bare q is the normal TUI close key, but accidental exits are costly
        during cluster triage. The first q shows a short toast; another q while
        that toast window is active takes the real quit path.
        """
        now = monotonic()
        if now <= self._quit_hint_deadline:
            self._quit_hint_deadline = 0.0
            self.action_quit()
            return
        timeout = 4
        self._quit_hint_deadline = now + timeout
        self.notify("Press q again to quit", title="Quit?", timeout=timeout)

    def _cancel_pending_quit(self, *, announce: bool = True) -> bool:
        """Settle a pending quit-hint window; True when one was pending.

        Every interaction that moves the user onto something else (search,
        menu focus, modals, Esc from the sidebar) routes through here so a
        stale window can never turn a later Enter into a surprise exit."""
        if monotonic() > self._quit_hint_deadline:
            return False
        self._quit_hint_deadline = 0.0
        if announce:
            try:
                self.clear_notifications()
            except Exception:
                pass
            self.notify("quit cancelled", timeout=2)
        return True

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Disable the Enter quit-confirm binding unless a quit is genuinely
        pending on the base dashboard. While no hint window is active, while a
        modal owns the screen, while a text input has focus, or while focus is
        anywhere inside the sidebar (its MENU buttons own Enter), the key must
        reach the focused widget — a priority app binding would otherwise
        consume it first."""
        if action == "confirm_quit":
            if monotonic() > self._quit_hint_deadline:
                return False
            try:
                if len(self.screen_stack) > 1 or isinstance(self.focused, Input):
                    return False
                if self.focused is not None and any(
                    getattr(node, "id", None) == "sidebar"
                    for node in self.focused.ancestors_with_self
                ):
                    return False
            except Exception:
                pass  # not running yet: no screen stack to consult
        return super().check_action(action, parameters)

    def action_confirm_quit(self) -> None:
        """Enter while the quit hint is pending takes the real quit path."""
        self._quit_hint_deadline = 0.0
        self.action_quit()

    def push_screen(self, screen, *args, **kwargs):
        """Opening any modal abandons a pending quit confirmation: the user
        has moved on to another interaction, so Enter inside (or right after)
        the modal must never fall through to quit."""
        self._cancel_pending_quit(announce=False)
        return super().push_screen(screen, *args, **kwargs)

    def action_quit(self) -> None:
        """Quit promptly: kill any in-flight kubectl before exiting so the worker
        thread isn't left blocking asyncio/interpreter teardown."""
        try:
            self.fetcher.cancel()
        except Exception:
            pass
        self.exit()

    def on_unmount(self) -> None:
        """Safety net for every exit path (q, Ctrl-C, error): abort fetches so
        the background worker can't hang the shutdown join."""
        try:
            self.fetcher.cancel()
        except Exception:
            pass

    def _apply_snapshot(self, snap: Snapshot, gen: Optional[int] = None) -> None:
        if gen is not None and gen != self._fetch_gen:
            return  # fetched under an old namespace/context scope: drop it
        if snap.error and not snap.nodes and not snap.pods:
            # full failure: keep previous frame, surface error
            self._notify_refresh_error(snap.error, full=True, errors=snap.errors)
            if not self._loaded:
                # no previous frame exists yet: a 4s toast alone would leave
                # the bare Loading row sitting there forever. The guidance row
                # carries the SAME aggregated detail as the toast so a
                # multi-source outage names every culprit on the one surface
                # that persists.
                self._render_startup_guidance(
                    self._refresh_error_detail(snap.error, snap.errors))
            return
        if snap.error:
            # partial failure: apply what we have, but say what is missing
            self._notify_refresh_error(snap.error, full=False, errors=snap.errors)
        else:
            self._last_refresh_error = ""
        first_load = not self._loaded
        self.snapshot = snap
        self._loaded = True
        # Only the live fetch path passes a generation token; synthetic frames
        # injected directly (cli --self-test/--snapshot, tests) pass none and
        # must never toast. This keeps the orientation hint to a real first load.
        if first_load and gen is not None:
            self._maybe_show_first_run_hint()
        # Feed rolling history every refresh for the trend meters. A zero used
        # value with a real capacity is a real 0% sample; only missing capacity
        # is treated as an untrustworthy dropout.
        s = snap.summary
        self._append_trend(self.cpu_hist, s.cpu_used_mcpu, s.cpu_cap_mcpu)
        self._append_trend(self.mem_hist, s.mem_used_mi, s.mem_cap_mi)
        self._render()

    def _maybe_show_first_run_hint(self) -> None:
        """Orient a first-time user once: name the core affordances via one toast.

        Gated so it never nags a returning user. Shown only when ALL hold:
          * it has not already fired this session (``_shown_first_run_hint``);
          * the active profile is the built-in generic one (no per-workload
            profile was selected — a power user who set one already knows the
            keys);
          * the config carries no obvious user customization — a cheap, robust
            signal that this is an out-of-the-box launch. The chosen heuristic is:
            the namespace scope is the lone default ``["default"]``, no
            profile-owned probes/panels were configured (no alertmanager URL, no
            health probes), and no ad-hoc filter is active (no live search term,
            only_problems off). ``hide_completed`` is the shipped default, so it
            is deliberately NOT treated as customization. Each check is a single
            attribute read, so the guard is cheap and does not depend on disk
            state.
        """
        if self._shown_first_run_hint:
            return
        self._shown_first_run_hint = True  # at most once per session, win or skip
        if (self.cfg.profile_name or "generic") != "generic":
            return
        if list(self.namespaces) != ["default"]:
            return
        if self.cfg.alertmanager_url or self.cfg.health_probes:
            return
        if self._search_term or self.cfg.only_problems:
            return
        self.notify(
            "Press b for sidebar (namespaces, profiles), o for options, / to search",
            title="Welcome to kutop",
            timeout=6,
        )

    @staticmethod
    def _refresh_error_detail(error: str, errors: "Optional[list]" = None) -> str:
        """One canonical aggregated failure line, shared by the refresh toast
        and the startup-guidance rows so both surfaces always agree.

        A single failure keeps the historical one-line shape; multiple
        failures aggregate as '2 failures: ns-a: ...; pvc: ...' (up to 3
        shown, 60 chars each, '+N more' beyond) so a multi-namespace RBAC
        problem names each broken source instead of only the first.
        """
        distinct: "list[str]" = []
        for msg in [error] + list(errors or []):
            msg = " ".join(str(msg).split())
            if msg and msg not in distinct:
                distinct.append(msg)
        if len(distinct) > 1:
            shown = [m if len(m) <= 60 else m[:59] + "…" for m in distinct[:3]]
            detail = f"{len(distinct)} failures: " + "; ".join(shown)
            if len(distinct) > 3:
                detail += f"; +{len(distinct) - 3} more"
            return detail
        return distinct[0] if distinct else error

    def _notify_refresh_error(
        self, error: str, *, full: bool,
        errors: "Optional[list]" = None,
    ) -> None:
        """Toast a refresh error once — not on every 5s retry with the same text.

        The dedup compares severity plus the aggregated detail (reset by the
        next clean refresh), so the same outage stays silent on retries but a
        degraded refresh escalating to a full failure with identical text
        still gets its error-severity toast.
        """
        detail = self._refresh_error_detail(error, errors)
        key = ("failed|" if full else "degraded|") + detail
        if key == self._last_refresh_error:
            return
        self._last_refresh_error = key
        if full:
            self.notify(f"refresh failed: {detail}", severity="error", timeout=4)
        else:
            self.notify(f"refresh degraded: {detail}", severity="warning", timeout=4)

    def _render_startup_guidance(self, error: str) -> None:
        """Persistent first-run guidance: shown only while NO snapshot has ever
        applied and the refresh keeps failing (cluster unreachable / bad
        kubeconfig). Re-rendered on every failed retry so the text tracks the
        latest error; the first successful snapshot re-renders the table and
        thereby clears it.
        """
        self._startup_error = error or "unknown error"
        try:
            mt = self.query_one("#main_table", DataTable)
        except Exception:
            return
        short = " ".join((error or "unknown error").split())
        if len(short) > 140:
            short = short[:139] + "…"
        # Name the kube context on the first row so a beginner can tell "right
        # cluster, unreachable" from "wrong context" (resolved exactly as the
        # sidebar/confirm modals display it; '' -> 'current' like _display_context
        # callers).
        ctx = self._display_context() or "current"
        pad = ["-"] * (len(self.cfg.visible_columns()) - 1)
        mt.clear()
        mt.add_row(
            Text(f"cluster unreachable (context: {ctx}): {short}", style="bold red"),
            *pad, key="startup_error",
        )
        mt.add_row(Text("check: kubectl get nodes", style="bold yellow"),
                   *pad, key="startup_hint")
        mt.add_row(Text(f"retrying every {self.interval:g}s...", style="dim"),
                   *pad, key="startup_retry")

    def _repopulate_unloaded_table(self, mt: DataTable) -> None:
        """A column rebuild clears every row; before the first snapshot the
        table must keep its loading row or startup guidance, not go blank."""
        if self._loaded:
            return
        if self._startup_error:
            self._render_startup_guidance(self._startup_error)
            return
        mt.add_row(Text("Loading cluster snapshot...", style="bold yellow"),
                   *(["-"] * (len(self.cfg.visible_columns()) - 1)), key="loading")

    @staticmethod
    def _append_trend(hist: deque[int], used: int, cap: int) -> None:
        """Append a clamped used/cap percent, carrying only missing-cap dropouts.

        When cap is known, ``used == 0`` is a real 0% sample and must update the
        top "now" readout. Carry-forward is only for cap dropouts, where the
        current percentage cannot be computed.
        """
        if cap > 0:
            hist.append(max(0, min(100, model.pct(used, cap))))
        elif hist:
            hist.append(hist[-1])
        # else: no prior value and no trustworthy cap -> append nothing

    def _reset_trend_history(self) -> None:
        """Drop CPU/MEM history when the watched cluster scope changes."""
        self.cpu_hist.clear()
        self.mem_hist.clear()

    # ── rendering ──────────────────────────────────────────────────────────────
    def _render(self) -> None:
        snap = self.snapshot
        s = snap.summary

        sb = self.query_one("#summary_bar", SummaryBar)
        if sb.style_mode != self.cfg.summary_style:
            sb.set_style_mode(self.cfg.summary_style)
        sb.update_summary(
            s,
            show_alerts=bool(self.cfg.alertmanager_url),
            cpu_thresh=self.cfg.threshold("cpu"),
            mem_thresh=self.cfg.threshold("mem"),
        )

        cpu_detail = f"{model.fmt_cpu(s.cpu_used_mcpu)}/{model.fmt_cpu(s.cpu_cap_mcpu)}"
        mem_detail = f"{model.fmt_mem(s.mem_used_mi)}/{model.fmt_mem(s.mem_cap_mi)}"
        self.query_one("#cpu_trend", TrendGraph).update_trend(list(self.cpu_hist), cpu_detail)
        self.query_one("#mem_trend", TrendGraph).update_trend(list(self.mem_hist), mem_detail)

        self._render_main_table()
        self._render_events()
        self._render_pvc()
        self._render_alerts()
        self._render_plugin_panels()

    def _render_alerts(self) -> None:
        try:
            at = self.query_one("#alerts_panel", DataTable)
        except Exception:
            return
        # toggled on but unconfigured: show why it's empty instead of nothing
        if not self.cfg.alertmanager_url:
            at.border_title = "ALERTS"
            at.clear()
            at.add_row(
                Text("set probes.alertmanager_url to enable", style="yellow"),
                "", "", "", key="hint",
            )
            return
        alerts = list(self.snapshot.alerts)
        at.border_title = f"ALERTS · {len(alerts)} firing" if alerts else "ALERTS"
        saved = self._saved_row_key(at)
        sx, sy = at.scroll_x, at.scroll_y
        at.clear()
        if not alerts:
            at.add_row(Text("no active alerts", style="green"), "", "", "", key="none")
            self._restore_row(at, saved, sx, sy)
            return
        # most severe first, then by name
        order = {"critical": 0, "error": 0, "warning": 1, "warn": 1, "info": 2}
        ordered = sorted(
            alerts, key=lambda a: (order.get((a.severity or "").lower(), 3), a.name)
        )
        for i, al in enumerate(ordered):
            st = _severity_style(al.severity)
            since = fmt_age(age_seconds(al.starts_at)) if al.starts_at else "-"
            at.add_row(
                Text(al.name, style=st),
                Text(al.severity or "-", style=st),
                Text(al.resource or "-", style="dim"),
                since,
                key=f"al:{i}",
            )
        self._restore_row(at, saved, sx, sy)

    def _render_plugin_panels(self) -> None:
        """Let enabled plugins update their mounted custom panels."""
        for plugin in self._all_plugins():
            panel_id = getattr(plugin, "panel_id", "")
            if not panel_id:
                continue
            try:
                panel = self.query_one(f"#{panel_id}")
                renderer = getattr(plugin, "render", None)
                if callable(renderer):
                    renderer(panel, self.snapshot)
            except Exception:
                pass

    def _effective_filter(self) -> str:
        """The active runtime name filter, exactly as the user typed it
        (stripped). Display sites (empty-state row, sidebar) use this so a
        regex term is shown verbatim — matching is case-insensitive regardless,
        so lowercasing the displayed term would only mislead (e.g. ``[A-Z]``)."""
        return (self._search_term or "").strip()

    # Characters that, when present in a term, make us attempt a regex match.
    _REGEX_META = frozenset(r".^$*+?[]{}|()\\")
    # Cap a term's length before treating it as a regex; together with the
    # nested-quantifier guard this bounds catastrophic backtracking that would
    # otherwise freeze the render thread (Python's re has no match timeout).
    _REGEX_MAX_LEN = 200

    @staticmethod
    def _has_nested_quantifier(pattern: str) -> bool:
        """True for the catastrophic-backtracking family — a quantifier applied
        to a group whose body already holds an unbounded quantifier (``(a+)+``,
        ``(.*)*``, ``(a{1,5})+``). Such a term is matched as a plain substring
        instead of being run, unbounded, on the UI thread."""
        stack = [False]  # per open group: does its body hold an unbounded quant?
        i, n = 0, len(pattern)
        while i < n:
            c = pattern[i]
            if c == "\\":
                i += 2
                continue
            if c == "(":
                stack.append(False)
            elif c == ")":
                inner = stack.pop() if len(stack) > 1 else False
                nxt = pattern[i + 1] if i + 1 < n else ""
                # tuple membership, not `nxt in "*+{"` — an empty nxt (group at
                # end of pattern) is a substring of every str and would wrongly
                # flag a safe, unquantified group like ``(a+)``.
                quantified = nxt in ("*", "+", "{")
                if inner and quantified:
                    return True
                if quantified and stack:
                    stack[-1] = True  # a quantified group bubbles up to its parent
            elif c in ("*", "+", "{"):
                stack[-1] = True
            i += 1
        return False

    @classmethod
    def _safe_regex(cls, term: str):
        """A compiled case-insensitive regex for ``term``, or ``None`` to fall
        back to substring. Returns ``None`` for a plain term, an invalid
        pattern, an over-long term, or a catastrophic-backtracking shape — so
        the matcher can never raise or hang the render path."""
        if not term or len(term) > cls._REGEX_MAX_LEN:
            return None
        if not any(ch in cls._REGEX_META for ch in term):
            return None
        if cls._has_nested_quantifier(term):
            return None
        try:
            return re.compile(term, re.IGNORECASE)
        except re.error:
            return None

    @classmethod
    def _term_is_regex(cls, term: str) -> bool:
        """True when ``term`` is treated as a (safe) regex rather than a plain
        substring. A bad/over-long/catastrophic term is matched as a substring."""
        return cls._safe_regex(term) is not None

    def _compile_filter(self, term: str):
        """Return a ``name -> bool`` matcher for ``term``, memoized on the term.

        A safe regex (metacharacter present, compiles, not catastrophic) is
        matched case-insensitively with ``re.search`` on a length-bounded name;
        anything else falls back to the historical case-insensitive substring
        test. Never raises into the render path and never runs an unbounded
        backtracking match. The matcher and the regex/plain decision are cached
        together so every per-render call site compiles at most once per term.
        """
        cached_term, cached_matcher, _ = self._filter_cache
        if cached_matcher is not None and cached_term == term:
            return cached_matcher

        rx = self._safe_regex(term)
        if rx is not None:
            matcher = lambda name: bool(rx.search(name[:256]))  # noqa: E731
        else:
            sub = term.lower()
            matcher = lambda name: sub in name.lower()  # noqa: E731
        self._filter_cache = (term, matcher, rx is not None)
        return matcher

    def _filter_is_regex(self, term: str) -> bool:
        """Whether the active term is matched as a regex — reads the matcher
        cache so the SEARCH-panel title costs no extra compile."""
        if not term:
            return False
        self._compile_filter(term)
        cached_term, _, is_rx = self._filter_cache
        return bool(is_rx) and cached_term == term

    @staticmethod
    def _is_problem(pod: Pod) -> bool:
        """A pod is a 'problem' if not cleanly Running / has restarts / oom / crash."""
        if pod.oomkilled or pod.crashloop or pod.restarts > 0:
            return True
        if pod.phase not in ("Running", "Succeeded"):
            return True
        if pod.phase == "Running":
            parts = (pod.ready or "").split("/")
            if not (len(parts) == 2 and parts[0] == parts[1] and parts[0] not in ("", "0")):
                return True
        return False

    @staticmethod
    def _is_completed(pod: Pod) -> bool:
        # "Finished/dead" pods that hide_completed (default on) removes: completed
        # jobs, evicted/failed pods, and pods being deleted (Terminating). This is
        # what keeps a dead pod from lingering beside its live replacement.
        return pod.terminating or pod.phase in ("Succeeded", "Completed", "Failed")

    def _visible_pods(self, pods: list[Pod]) -> list[Pod]:
        """Apply config-driven + live filters (name / hide_completed / only_problems)."""
        term = (self._search_term or "").strip()
        matcher = self._compile_filter(term) if term else None
        out = []
        for pd in pods:
            if matcher is not None and not matcher(pd.name):
                continue
            if self.cfg.hide_completed and self._is_completed(pd):
                continue
            if self.cfg.only_problems and not self._is_problem(pd):
                continue
            out.append(pd)
        return out

    def _render_main_table(self) -> None:
        snap = self.snapshot
        mt = self.query_one("#main_table", DataTable)
        ctx = self.render_ctx
        cols = [self.column_registry[k] for k in self.cfg.visible_columns()]

        # preserve cursor + scroll
        saved_key = self._saved_row_key(mt)
        sx, sy = mt.scroll_x, mt.scroll_y
        mt.clear()

        def node_cells(node) -> list:
            out = []
            for spec in cols:
                if spec.node_accessor is not None:
                    out.append(spec.node_accessor(node, ctx))
                else:
                    out.append(Text("·", style="grey37"))
            return out

        def pod_cells(pod) -> list:
            return [spec.pod_accessor(pod, ctx) for spec in cols]

        def sep_row(text: str, style: str = "dim") -> list:
            return [Text(text, style=style)] + \
                   [Text("·", style="grey37") for _ in cols[1:]]

        pods = self._visible_pods(snap.pods)

        if self.cfg.group_by_node:
            # ── grouped view: node header rows, then that node's pods ──────────
            nodes = list(snap.nodes)
            node_names = {n.name for n in nodes}
            for n in nodes:
                node_pods = self._sorted_pods([pd for pd in pods if pd.node == n.name])
                mt.add_row(*node_cells(n), key=f"node:{n.name}")
                for pod in node_pods:
                    mt.add_row(*self._indent_pod_cells(pod, cols, ctx),
                               key=f"pod:{pod.namespace}/{pod.name}")
            unscheduled = self._sorted_pods(
                [pd for pd in pods if pd.node not in node_names]
            )
            if unscheduled:
                mt.add_row(*sep_row("◇ (unscheduled / no node)"),
                           key="node:__unscheduled__")
                for pod in unscheduled:
                    mt.add_row(*self._indent_pod_cells(pod, cols, ctx),
                               key=f"pod:{pod.namespace}/{pod.name}")
        else:
            # ── flat view: one global sorted list (priority/sort order) ────────
            for pod in self._sorted_pods(pods):
                mt.add_row(*pod_cells(pod), key=f"pod:{pod.namespace}/{pod.name}")

        if mt.row_count == 0:
            mt.add_row(*sep_row(self._empty_state_message()))

        self._restore_row(mt, saved_key, sx, sy)
        self._sync_sidebar_state()

    def _empty_state_message(self) -> str:
        """Actionable text for the main table's single empty-state guidance row.

        A live search term is the most common reason a beginner sees nothing, so
        it wins: name the term and how to clear it. Otherwise (no rows at all)
        name the watched namespaces and any active filters so the user knows what
        is hiding pods and which key changes it. Falls back to the bare
        '(no pods)' only when nothing is scoped to mention.
        """
        term = self._effective_filter()
        if term:
            return f'no pods match "{term}" — esc to clear'
        active = []
        if self.cfg.hide_completed:
            active.append("hide_completed on")
        if self.cfg.only_problems:
            active.append("only_problems on")
        ns = list(self.namespaces)
        if ns:
            base = f"no pods in [{', '.join(ns)}]"
            if active:
                base += f" ({', '.join(active)})"
            return f"{base} — b to change namespaces"
        if active:
            return f"no pods ({', '.join(active)}) — b to change filters"
        return "(no pods)"

    def _indent_pod_cells(self, pod: Pod, cols, ctx) -> list:
        """Pod cells for the grouped view, with the name cell indented under node."""
        cells = [spec.pod_accessor(pod, ctx) for spec in cols]
        if cells:
            first = Text("  ")
            try:
                first.append_text(cells[0])
            except Exception:
                first.append(str(cells[0]))
            cells[0] = first
        return cells

    def _pod_name_cell(self, pod: Pod) -> Text:
        """Build the highlighted NODE/POD name cell (used by the column registry).

        The cell is built at full length (glyph + name + status/annotations) then
        fit to ``cfg.name_width`` cells — overflow is ellipsised, a wider column
        reveals more of the name. The leading "  " indent is part of the cell so
        the fit accounts for it.
        """
        glyph, gstyle = _status_glyph(pod)
        disp = pod.name
        name = Text("  ")
        name.append(glyph, style=gstyle)
        name.append(" ")
        if pod.oomkilled:
            name.append(disp, style="bold red")
            name.append(" OOMKilled", style="bold white on red")
        elif pod.crashloop:
            name.append(disp, style="bold red")
            name.append(" CrashLoop", style="bold white on red")
        elif pod.phase == "Pending":
            name.append(disp, style="bold yellow")
            name.append(" Pending", style="black on yellow")
        else:
            name.append(disp)
        # only annotate ns inline when the dedicated 'namespace' column is hidden
        if len(self.namespaces) > 1 and "namespace" not in self.cfg.visible_columns():
            name.append(f" [{pod.namespace}]", style="dim")
        name.append(f" ({pod.ready})", style="dim")
        return _fit_cell(name, self.cfg.name_width)

    def _sort_value(self, pod: Pod):
        """Return the comparable key for the active sort_key.

        Returns a tuple whose first element drives ordering and whose second is
        the pod name (stable tiebreak). For descending sorts the renderer flips
        the result; we keep the tiebreak ascending so equal rows stay readable.
        """
        key = self.cfg.sort_key
        if key == "cpu":
            return (pod.cpu_mcpu,)
        if key == "mem":
            return (pod.mem_mi,)
        if key == "cpu_pct":
            return (pod.cpu_pct,)
        if key == "mem_pct":
            return (pod.mem_pct,)
        if key == "restarts":
            return (pod.restarts,)
        if key == "phase":
            return (pod.phase,)
        if key == "node":
            return (pod.node,)
        if key == "namespace":
            return (pod.namespace,)
        if key == "name":
            return (pod.name,)
        if key == "age":
            # numeric by seconds; unknown ages sort last (treated as 0 = newest).
            # NOTE: larger age = older. Ascending shows youngest first.
            return (pod.age_secs or 0,)
        if key == "storage":
            return (pod.storage_used_mi if pod.storage_used_mi is not None else -1,)
        if key == "owner":
            return (pod.owner_kind,)
        # default: profile-driven weight (priority)
        return (self.profile.weight_for(pod.name),)

    def _sorted_pods(self, pods: list[Pod]) -> list[Pod]:
        desc = self.cfg.sort_desc
        # numeric keys sort high->low by default when descending is requested;
        # for the profile "priority" sort the natural (ascending weight) order is
        # the intended default, so sort_desc simply reverses whatever it produces.
        ordered = sorted(pods, key=lambda pd: (self._sort_value(pd), pd.name))
        if desc:
            ordered.reverse()
        return ordered

    def _render_events(self) -> None:
        et = self.query_one("#events_table", DataTable)
        saved = self._saved_row_key(et)
        sx, sy = et.scroll_x, et.scroll_y
        et.clear()
        # warnings first, then most recent; show a manageable tail
        evs = sorted(self.snapshot.events,
                     key=lambda e: (e.type != "Warning", ), )
        for i, ev in enumerate(evs[:30]):
            style = "bold yellow" if ev.type == "Warning" else "dim"
            et.add_row(
                _fmt_event_ts(ev.ts_utc, self.tz),
                Text(ev.name[:24], style=style),
                Text(ev.reason[:18], style=style),
                str(ev.count),
                key=f"ev:{i}",
            )
        self._restore_row(et, saved, sx, sy)

    def _render_pvc(self) -> None:
        pt = self.query_one("#pvc_table", DataTable)
        pvc_warn, pvc_crit = self.cfg.threshold("pvc")
        saved = self._saved_row_key(pt)
        sx, sy = pt.scroll_x, pt.scroll_y
        pt.clear()
        for pvc in self.snapshot.pvcs:
            used_pct = pvc.used_pct  # None when usage unavailable -> '-'
            cap_s = model.fmt_mem(pvc.capacity_mi) if pvc.capacity_mi else "-"
            if pvc.used_mi is not None:
                storage = f"{model.fmt_mem(pvc.used_mi)} / {cap_s} ({used_pct}%)"
            else:
                storage = f"- / {cap_s}"
            pt.add_row(
                pvc.name[:32],
                storage,
                bar_gauge(used_pct, pvc_warn, pvc_crit),
                pvc.storage_class or "-",
                key=f"pvc:{pvc.namespace}/{pvc.name}",
            )
        self._restore_row(pt, saved, sx, sy)

    # ── cursor/scroll preservation helpers ─────────────────────────────────────
    @staticmethod
    def _saved_row_key(table: DataTable):
        if table.row_count > 0 and table.cursor_row >= 0:
            try:
                k = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
                if k.value not in (None, "loading"):
                    return k
            except Exception:
                return None
        return None

    @staticmethod
    def _restore_row(table: DataTable, saved_key, sx, sy) -> None:
        if saved_key is not None and saved_key in table.rows:
            try:
                idx = list(table.rows.keys()).index(saved_key)
                # scroll=False: keep the cursor on its logical row but DON'T yank
                # the viewport to it — a user who scrolled away must stay put.
                # (Default scroll=True is what snapped the view back every refresh.)
                table.move_cursor(row=idx, animate=False, scroll=False)
            except Exception:
                pass

        def _restore_scroll() -> None:
            try:
                table.scroll_to(x=sx, y=sy, animate=False)
            except Exception:
                pass

        _restore_scroll()
        # Re-assert after the refresh to beat any deferred "scroll cursor into
        # view" that DataTable may schedule, which otherwise causes the delayed
        # snap-back the user sees a few seconds after scrolling.
        try:
            table.call_after_refresh(_restore_scroll)
        except Exception:
            pass

    # ── config persistence ──────────────────────────────────────────────────────
    def _sync_cfg_from_app(self) -> None:
        """Scrub transient state from self.cfg before saving.

        Panel/sort/namespace state lives directly on self.cfg (see the
        properties above), so the only thing left to normalise is the live
        search term, which must never persist.
        """
        self.cfg.name_filter = ""

    def _persist_state(self) -> None:
        """Persist current config to the active config file (--config or default).

        Best effort — failure must never disturb the UI.
        """
        self._sync_cfg_from_app()
        try:
            save_config(self.cfg, self._config_path, profile=self.profile)
        except Exception:
            pass  # unwritable path or other I/O error — don't disturb the UI

    def commit_name_width(self, width: int) -> None:
        """Adopt a drag-resized NODE/POD column width and persist it.

        Called by :class:`ResizableDataTable` on mouse-up: clamp the value, store
        it on the live config, re-pin the column, and persist so the width
        survives a relaunch. Re-renders the table so the cell-fit reflects the
        final width.
        """
        from ..config import clamp_name_width
        self.cfg.name_width = clamp_name_width(width)
        try:
            mt = self.query_one("#main_table", DataTable)
            idx = self._name_column_index(mt)
            if idx is not None:
                self._apply_name_width(mt, idx)
        except Exception:
            pass
        self._persist_state()
        if self._loaded:
            self._render_main_table()

    # ── live config application (from the Options modal) ─────────────────────────
    def _adopt_config(self, cfg: Config, *, persist: bool) -> None:
        """Adopt an edited Config: re-render everything live, optionally persist."""
        prev_ns = list(self.namespaces)
        prev_context = self.context or ""
        prev_alertmgr = self.cfg.alertmanager_url
        prev_probes = list(self.cfg.health_probes)

        if cfg.name_filter:
            self._search_term = str(cfg.name_filter).strip()
        cfg.name_filter = ""
        self.cfg = cfg
        cfg.theme = self._coerce_theme(cfg.theme)
        if self.theme != cfg.theme:
            self.theme = cfg.theme
        self.apply_theme_chrome()
        # Mirror the adopted context onto self.context up front so the sidebar
        # CONTEXT dropdown (refreshed via _sync_sidebar_state below) reflects the
        # NEW cluster immediately. Change detection still uses prev_context, which
        # was snapshotted before this assignment; the fetcher re-wiring + refetch
        # stay in the context-changed block at the end of this method.
        self.context = cfg.context or None
        self.tz = _resolve_tz(cfg.timezone)

        # probe (re)wiring: if the alertmanager URL or health probes changed,
        # update the fetcher so the next refresh picks them up live.
        if cfg.alertmanager_url != prev_alertmgr or cfg.health_probes != prev_probes:
            self.fetcher.alertmanager_url = cfg.alertmanager_url
            self.fetcher.health_probes = self._probe_specs(cfg)

        # summary style change -> reflow the header tiles. Guard the lookup: a
        # context switch can re-enter _adopt_config (sidebar -> set_context) before
        # the dashboard is mounted, or during a transient teardown, where
        # #summary_bar is momentarily absent — degrade gracefully instead of
        # raising NoMatches and freezing/crashing the live cluster switch.
        try:
            sb = self.query_one("#summary_bar", SummaryBar)
            if sb.style_mode != cfg.summary_style:
                sb.set_style_mode(cfg.summary_style)
        except NoMatches:
            pass

        # rebuild columns if the visible set/order OR the sort indicator changed.
        # Compare against the table's ACTUAL header labels (which now carry the
        # ▲/▼ sort glyph) so a sort_key/sort_desc change re-stamps the header.
        # Same transient-unmount guard as the summary bar above.
        try:
            mt = self.query_one("#main_table", DataTable)
            # compare PLAIN header text on both sides: _column_label returns a
            # rich Text for the name column, and Text != str is always True —
            # which made every Options change rebuild the table (losing the
            # cursor and scroll position) even when the columns were unchanged.
            want = [
                lbl.plain if isinstance(lbl, Text) else str(lbl)
                for lbl in (self._column_label(k) for k in cfg.visible_columns())
            ]
            have = [col.label.plain for col in mt.ordered_columns]
            if want != have:
                self._build_main_columns(mt)
                self._repopulate_unloaded_table(mt)
            else:
                # columns unchanged but the adopted config may carry a different
                # name_width (e.g. hot-reload 'R' or an edited config) — re-pin it.
                idx = self._name_column_index(mt)
                if idx is not None:
                    self._apply_name_width(mt, idx)
        except NoMatches:
            pass

        # Refresh cadence is fixed; pin it so an adopted/legacy config can never
        # change the timer or desync the live value.
        cfg.interval = REFRESH_INTERVAL_SECS
        self.interval = REFRESH_INTERVAL_SECS

        self.apply_panel_visibility(persist=persist)
        self._sync_sidebar_state()
        if self._loaded:
            self._render()

        context_changed = (cfg.context or "") != prev_context

        # namespace/context change -> refetch + re-sync the sidebar checkboxes so the
        # sidebar (primary control) and the Options modal never contradict.
        if list(cfg.namespaces) != prev_ns or context_changed:
            self._reset_trend_history()
            self._bump_fetch_gen()  # drop any in-flight old-scope fetch result
            self.fetcher.namespaces = list(cfg.namespaces)
            self.fetcher.context = cfg.context or None
            self.context = cfg.context or None
            if list(cfg.namespaces) != prev_ns:
                self._sync_sidebar_ns()
            self._request_refresh()

        if persist:
            try:
                # profile= matters: without it _config_for_persist takes the
                # conservative legacy branch under an active profile and
                # strips the user's saved namespaces/context/timezone too.
                save_config(self.cfg, self._config_path, profile=self.profile)
            except Exception:
                pass

    def apply_config(self, cfg: Config) -> None:
        """Adopt an edited Config: re-render everything live + persist."""
        self._adopt_config(cfg, persist=True)

    def export_config(self, cfg: Optional[Config] = None) -> Optional[str]:
        """Write the full config skeleton to the user file; return its path."""
        target = cfg or self.cfg
        try:
            path = save_config(target, self._config_path, profile=self.profile)
            self.notify(f"config exported -> {path}")
            return path
        except Exception as exc:
            self.notify(f"export failed: {exc}", severity="error")
            return None

    def preview_theme(self, theme: str) -> None:
        """Preview a Textual theme without changing saved config."""
        self.theme = self._coerce_theme(theme)

    def commit_theme(self, theme: str) -> None:
        """Persist a selected Textual theme."""
        self.cfg.theme = self._coerce_theme(theme)
        self.theme = self.cfg.theme
        try:
            save_config(self.cfg, self._config_path, profile=self.profile)
        except Exception:
            pass

    def action_open_menu(self) -> None:
        """Hamburger entry point: the sidebar IS the menu (issue #2 unification).

        Hidden sidebar -> reveal it; visible sidebar -> land focus on its first
        MENU control, so the click always reaches the unified command surface.
        """
        # Reaching for the menu abandons a pending quit — the focused MENU
        # button owns the next Enter (also gated in check_action).
        self._cancel_pending_quit(announce=False)
        sb = self.query_one("#sidebar", SidebarPanel)
        if sb.has_class("-hidden"):
            self.action_toggle_sidebar()
        else:
            sb.focus_menu()

    def action_open_options(self) -> None:
        self._sync_cfg_from_app()
        # Context names come from the discovery cache only — _context_options()
        # can shell out to kubectl (up to 2s) and this runs on the UI thread.
        # When the cache is empty, kick the discovery worker so a reopened
        # modal has the list.
        if not self._discovered_contexts and self._discover_namespaces:
            self.run_worker(self._discover_ns_worker, thread=True,
                            exclusive=False, group="ns")
        # hand the modal a copy so a cancelled edit can't half-mutate live state;
        # apply_config() adopts the working copy on every change anyway. The
        # discovered namespace list powers the multi-select.
        self.push_screen(
            OptionsModal(
                copy.deepcopy(self.cfg),
                discovered_ns=list(self._discovered_ns),
                context_names=self._cached_context_options(),
                themes=self._theme_options(),
            )
        )

    # ── panel visibility ─────────────────────────────────────────────────────────
    def apply_theme_chrome(self) -> None:
        """Apply visual theme toggles that are stored in Config.view."""
        on = bool(self.cfg.panel_backgrounds)
        self.set_class(on, "-panel-backgrounds-on")
        for screen in list(getattr(self, "screen_stack", [])):
            try:
                screen.set_class(on, "-panel-backgrounds-on")
            except Exception:
                continue

    def apply_panel_visibility(self, *, persist: bool = False) -> None:
        # NOTE: persist defaults to False so render-time callers (on_mount,
        # live re-renders) never rewrite the user's config. Only genuine user
        # actions (panel toggles, Options apply) pass persist=True. Persisting
        # on startup once corrupted configs by overwriting the loaded cfg.
        # Panel toggles touch many mounted widgets. A context switch can re-enter
        # this via set_context -> _adopt_config before the dashboard has mounted
        # (or during a transient teardown), where the panel widgets are momentarily
        # absent — degrade gracefully instead of raising NoMatches and crashing the
        # live cluster switch. The cfg mirroring above already happened, so state
        # stays correct and the next mounted render reflects it.
        try:
            self.query_one("#summary_bar").set_class(not self.cfg.show_summary, "-hidden")
            self.query_one("#trends").set_class(not self.cfg.show_trends, "-hidden")
            self.query_one("#main_table").set_class(not self.cfg.show_podtable, "-hidden")
            self.query_one("#events_table").set_class(not self.show_events, "-hidden")
            self.query_one("#pvc_table").set_class(not self.show_pvc, "-hidden")
            self.query_one("#bottom_box").set_class(
                not (self.show_events or self.show_pvc), "-hidden"
            )
            # Show the alerts panel whenever it is toggled on; when no
            # alertmanager_url is configured it renders a setup hint (see
            # _render_alerts) instead of being silently hidden.
            alerts_on = self.show_alerts
            self.query_one("#alerts_panel").set_class(not alerts_on, "-hidden")
            # Plugin panels are best-effort: absent plugins mount no panel, enabled
            # plugins are toggled through their declared panel id.
            any_plugin_on = self._apply_plugin_panel_visibility()
            # collapse the whole top row when no panel is shown (no empty band)
            self.query_one("#top_panels").set_class(
                not (alerts_on or any_plugin_on), "-hidden"
            )
        except NoMatches:
            pass
        if persist:
            self._persist_state()
        self._sync_sidebar_state()

    def _display_context(self) -> str:
        """Context name for the UI: explicit override, else the resolved kubectl
        current-context, else '' (the sidebar then falls back to 'current')."""
        return self.context or self._resolved_context or ""

    def _sidebar_key_context(self) -> tuple[str, list[tuple[str, str]]]:
        """Return the contextual key hints for the sidebar Keys panel.

        The footer remains the global shortcut summary. This panel intentionally
        shows only the active work context so it does not become a duplicated
        help dump.
        """
        try:
            search_visible = not self.query_one("#search_bar", SearchBar).has_class("-hidden")
        except Exception:
            search_visible = False
        if search_visible:
            # Mirror _compile_filter's decision so the title reflects how the
            # active term is actually matched (regex vs. plain substring).
            term = (self._search_term or "").strip()
            title = "SEARCH (regex)" if self._filter_is_regex(term) else "SEARCH"
            return (
                title,
                [
                    (_binding_key("search"), "Edit search"),
                    ("enter", "Keep filter"),
                    (_binding_key("clear_search"), "Clear"),
                ],
            )

        if getattr(self.focused, "id", "") == "events_table":
            return (
                "EVENTS",
                [
                    ("enter", "Details"),
                    (_binding_key("toggle_events"), "Hide events"),
                ],
            )

        pod = self._focused_pod()
        if pod:
            rows = [
                (_binding_key("show_logs"), "Logs"),
                (_binding_key("describe_pod"), "Describe"),
                (_binding_key("show_yaml"), "YAML"),
                (_binding_key("shell_pod"), "Shell"),
            ]
            delete_label = "Delete" if self.allow_destructive else "Delete disabled"
            rows.append((_binding_key("delete_pod"), delete_label))
            restart_label = ("Restart" if self.allow_destructive
                             else "Restart disabled")
            rows.append((_binding_key("restart_pod"), restart_label))
            return ("POD ROW", rows)

        return (
            "DASHBOARD",
            [
                (_binding_key("cycle_sort"), "Sort"),
                (_binding_key("toggle_group"), "Group"),
                (_binding_key("search"), "Search"),
                (_binding_key("toggle_sidebar"), "Sidebar"),
            ],
        )

    def _sidebar_state(self, key_context: str = "DASHBOARD",
                       key_rows: "Optional[list]" = None) -> SidebarState:
        """The app state the sidebar mirrors, as one value object (the single
        source for both the initial compose and every later sync)."""
        return SidebarState(
            selected=list(self.namespaces),
            show_events=self.show_events,
            show_pvc=self.show_pvc,
            show_summary=self.cfg.show_summary,
            show_trends=self.cfg.show_trends,
            show_alerts=self.show_alerts,
            show_health=self.show_health,
            show_keys=self.cfg.show_keys,
            sort_key=self.cfg.sort_key,
            sort_desc=self.cfg.sort_desc,
            group_by_node=self.cfg.group_by_node,
            allow_delete=self.allow_destructive,
            profile_name=self.cfg.profile_name,
            remember_profile=self.cfg.remember_profile_per_context,
            interval=self.interval,
            context=self._display_context(),
            name_filter=self._effective_filter(),
            key_context=key_context,
            key_rows=list(key_rows or []),
        )

    def _sync_sidebar_state(self) -> None:
        """Mirror app state into the sidebar controls without rebuilding rows."""
        try:
            sidebar = self.query_one("#sidebar", SidebarPanel)
        except Exception:
            return
        key_context, key_rows = self._sidebar_key_context()
        sidebar.update_state(self._sidebar_state(key_context, key_rows))

    def _apply_plugin_panel_visibility(self) -> bool:
        """Show/hide each enabled plugin's panel; return True if any is visible.

        Generic: the core toggles a plugin panel using only its declared
        ``panel_id`` plus the matching panel toggle. The health plugin maps to the
        ``show_health`` toggle; other plugins default to shown when enabled.
        """
        any_on = False
        for plugin in self._all_plugins():
            panel_id = getattr(plugin, "panel_id", "")
            if not panel_id:
                continue
            # map known panel toggles; unknown plugin panels show when enabled
            on = self.show_health if panel_id == "health_panel" else True
            try:
                self.query_one(f"#{panel_id}").set_class(not on, "-hidden")
            except Exception:
                continue  # panel not mounted (plugin absent) -> nothing to toggle
            if on:
                any_on = True
        return any_on

    def action_toggle_events(self) -> None:
        self.show_events = not self.show_events
        try:
            self.query_one("#chk_events", Checkbox).value = self.show_events
        except Exception:
            pass
        self.apply_panel_visibility(persist=True)

    def action_toggle_pvc(self) -> None:
        self.show_pvc = not self.show_pvc
        try:
            self.query_one("#chk_pvc", Checkbox).value = self.show_pvc
        except Exception:
            pass
        self.apply_panel_visibility(persist=True)

    def action_toggle_alerts(self) -> None:
        self.show_alerts = not self.show_alerts
        if self.show_alerts and not self.cfg.alertmanager_url:
            self.notify("alerts: set probes.alertmanager_url to enable",
                        severity="warning")
        self.apply_panel_visibility(persist=True)

    def action_toggle_health(self) -> None:
        self.show_health = not self.show_health
        if self.show_health and not self.cfg.health_probes:
            self.notify("health: add probes.health_probes to enable",
                        severity="warning")
        self.apply_panel_visibility(persist=True)

    def action_reload_config(self) -> None:
        """Re-read the user config file and apply it live (M5 hot-reload).

        Re-runs the same layered load the CLI used (defaults -> profile -> file)
        so an edit to ``~/.config/kutop/config.yaml`` takes effect without a
        restart. Robust: a missing/broken file falls back to defaults+profile and
        the app keeps running. Shows a toast either way.
        """
        try:
            cfg = load_config(profile=self.profile, user_path=self._config_path)
        except Exception as exc:
            self.notify(f"reload failed: {exc}", severity="error")
            return
        self.apply_config(cfg)
        for warning in cfg.load_warnings:
            self.notify(warning, severity="warning", timeout=8)
        src = self._config_path or "~/.config/kutop/config.yaml"
        self.notify(f"config reloaded from {src}")

    def action_toggle_sidebar(self) -> None:
        sb = self.query_one("#sidebar")
        hidden = not sb.has_class("-hidden")
        sb.set_class(hidden, "-hidden")

    def action_refresh(self) -> None:
        self._request_refresh()
        self.notify("refreshing...")

    # ── metrics-freshness readout (fixed; cadence is no longer adjustable) ────
    def _update_metrics_indicator(self) -> None:
        """Render the fixed top-right metrics-freshness readout.

        The refresh cadence is no longer user-adjustable, so this slot is a
        static, non-clickable label showing how often metrics-server refreshes
        the CPU/MEM numbers (its default ``--metric-resolution``). The dashboard
        re-polls every ``REFRESH_INTERVAL_SECS`` for problem signals; the metric
        values themselves only move this fast.
        """
        try:
            indicator = self.query_one("#metrics_indicator", Static)
        except Exception:
            return
        indicator.update(f"[dim]◷ metrics[/] [b cyan]{METRICS_RESOLUTION_SECS:g}s[/]")

    def action_cycle_sort(self) -> None:
        """Cycle the active sort_key through every sortable column."""
        keys = list(SORTABLE_KEYS)
        cur = self.cfg.sort_key if self.cfg.sort_key in keys else "priority"
        nxt = keys[(keys.index(cur) + 1) % len(keys)]
        self.set_sort_key(nxt)

    def action_toggle_sort_dir(self) -> None:
        """Flip ascending/descending on the current sort column."""
        self.cfg.sort_desc = not self.cfg.sort_desc
        self.notify(f"sort: {self.cfg.sort_key} {'▼' if self.cfg.sort_desc else '▲'}")
        self._persist_state()
        self._restamp_sort_header()
        if self._loaded:
            self._render_main_table()

    def set_sort_key(self, key: str) -> None:
        if key not in SORTABLE_KEYS:
            key = "priority"
        self.cfg.sort_key = key
        # keep the legacy sort_mode coherent for any old code path
        self.sort_mode = key if key in ("priority", "cpu", "mem", "name") else "priority"
        self.notify(f"sort: {key} {'▼' if self.cfg.sort_desc else '▲'}")
        self._persist_state()
        self._restamp_sort_header()
        self._sync_sidebar_state()
        if self._loaded:
            self._render_main_table()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Click a column header to sort by it (repeat click flips direction).

        Mirrors the `s` (cycle) / `S` (flip) keys. Columns added via add_columns in
        visible-column order, so column_index maps straight to that list.
        """
        if event.data_table.id != "main_table":
            return
        vis = self.cfg.visible_columns()
        idx = event.column_index
        if idx is None or idx < 0 or idx >= len(vis):
            return
        sort_key = COLUMN_TO_SORT_KEY.get(vis[idx])
        if not sort_key:
            return  # non-sortable column (e.g. ready/last_reason) — ignore click
        if self.cfg.sort_key == sort_key:
            self.action_toggle_sort_dir()
        else:
            self.cfg.sort_desc = False
            self.set_sort_key(sort_key)

    def _restamp_sort_header(self) -> None:
        """Re-apply column headers so the ▲/▼ indicator moves to the new column."""
        try:
            mt = self.query_one("#main_table", DataTable)
            self._build_main_columns(mt)
            self._repopulate_unloaded_table(mt)
        except Exception:
            pass

    # legacy shim: the sidebar still calls set_sort_mode(mode)
    def set_sort_mode(self, mode: str) -> None:
        self.set_sort_key(mode)

    def action_toggle_group(self) -> None:
        """Toggle the node-grouped (topology) view."""
        self.cfg.group_by_node = not self.cfg.group_by_node
        self.notify(f"group by node: {'on' if self.cfg.group_by_node else 'off'}")
        self._persist_state()
        self._sync_sidebar_state()
        if self._loaded:
            self._render_main_table()

    # ── search / filter (key '/') ───────────────────────────────────────────────
    def action_search(self) -> None:
        """Reveal the search bar and focus its input.

        Starting a search abandons a pending quit confirmation, so Enter that
        submits the filter can never double as a quit confirm."""
        self._cancel_pending_quit(announce=False)
        bar = self.query_one("#search_bar", SearchBar)
        bar.set_class(False, "-hidden")
        bar.set_value(self._search_term)
        bar.focus_input()
        self._sync_sidebar_state()

    def action_clear_search(self) -> None:
        """Clear the live search term and hide the bar.

        A pending quit hint is settled first: that Esc only cancels the quit,
        so a search filter never disappears in the same keypress.
        """
        if self._cancel_pending_quit():
            return
        if not self._search_term and self.query_one("#search_bar", SearchBar).has_class("-hidden"):
            return
        self._search_term = ""
        bar = self.query_one("#search_bar", SearchBar)
        bar.set_value("")
        bar.set_class(True, "-hidden")
        try:
            self.query_one("#main_table", DataTable).focus()
        except Exception:
            pass
        if self._loaded:
            self._render_main_table()
        self._sync_sidebar_state()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter the pod table as the user types in the search bar."""
        if event.input.id == "search_input":
            self._search_term = event.value
            self._sync_sidebar_state()
            if self._loaded:
                self._render_main_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            # keep the filter applied; move focus back to the table
            try:
                self.query_one("#main_table", DataTable).focus()
            except Exception:
                pass
            self._sync_sidebar_state()

    def on_data_table_row_highlighted(self, event) -> None:
        if getattr(event.data_table, "id", "") in ("main_table", "events_table"):
            self._sync_sidebar_state()

    def on_descendant_focus(self, event) -> None:
        """Re-resolve the sidebar KEYS context the moment focus moves (e.g.
        into the events table), not only when a table cursor changes. Focus
        shifts inside modals never change the context, so skip the rebuild."""
        if len(self.screen_stack) == 1:
            self._sync_sidebar_state()

    def set_namespaces(self, ns_list: list[str]) -> None:
        """Adopt the ticked namespace set from the sidebar checkboxes.

        Shows the COMBINED pods of every ticked namespace (the Fetcher merges a
        namespace list). Unticking all namespaces is a no-op selection-wise (we
        keep at least one watched so the table never goes permanently blank) but
        still re-syncs the checkboxes so the user sees the floor enforced.
        """
        ns_list = [n.strip() for n in ns_list if n and n.strip()]
        if not ns_list:
            # never drop to zero namespaces; restore the last known good set and
            # re-tick it so the UI reflects the enforced floor.
            self.notify("keep at least one namespace ticked", severity="warning")
            self._sync_sidebar_ns()
            return
        if ns_list == list(self.namespaces):
            return
        self.namespaces = ns_list
        self._bump_fetch_gen()  # drop any in-flight old-scope fetch result
        self.fetcher.namespaces = ns_list
        self._reset_trend_history()
        self.notify(f"namespaces: {', '.join(ns_list)}")
        self._persist_state()
        self._sync_sidebar_state()
        self._request_refresh()

    # legacy shim: a CSV string still routes through the list-based selector
    def change_namespaces(self, ns_csv: str) -> None:
        self.set_namespaces([n for n in ns_csv.split(",")])

    def _sync_sidebar_ns(self) -> None:
        """Re-tick the sidebar checkboxes to match ``self.namespaces`` (best effort).

        Used after a modal-driven namespace change or after the enforced
        at-least-one floor so the sidebar checkboxes never contradict the live
        watched set.
        """
        try:
            sidebar = self.query_one("#sidebar", SidebarPanel)
        except Exception:
            return
        opts = self._sidebar_ns_options(self._discovered_ns)
        sidebar.rebuild_namespaces(opts, list(self.namespaces))
        self._sync_sidebar_state()

    # ── focused-pod resolution for modals ──────────────────────────────────────
    def _focused_pod(self) -> Optional[Pod]:
        mt = self.query_one("#main_table", DataTable)
        if mt.row_count == 0 or mt.cursor_row < 0:
            return None
        try:
            key = mt.coordinate_to_cell_key((mt.cursor_row, 0)).row_key
            kv = str(key.value or "")
        except Exception:
            return None
        if not kv.startswith("pod:"):
            return None
        ident = kv.split(":", 1)[1]  # "<ns>/<name>"
        ns, _, name = ident.partition("/")
        for pd in self.snapshot.pods:
            if pd.namespace == ns and pd.name == name:
                return pd
        return None

    def action_show_logs(self) -> None:
        pod = self._focused_pod()
        if pod:
            status = pod.last_terminated_reason or ""
            if status and pod.last_exit_code is not None:
                status += f" exit={pod.last_exit_code}"
            self.push_screen(LogViewerModal(
                pod.name, pod.namespace, self.log_tail, self.context,
                containers=list(pod.container_names), status_line=status,
            ))
        else:
            self.notify("focus a pod row first", severity="warning")

    def _shell_cmd(self, pod: Pod) -> "list[str]":
        """argv for an interactive shell in the focused pod (bash, else sh)."""
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += [
            "exec", "-it", "-n", pod.namespace, pod.name, "--",
            "sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash || exec sh",
        ]
        return cmd

    def action_shell_pod(self) -> None:
        """Hand the real terminal to ``kubectl exec -it`` for the focused pod.

        The app suspends (terminal modes restored), the shell runs in the
        foreground, and the dashboard resumes with an immediate refetch when
        the shell exits. Headless contexts (tests/snapshots) reject suspend —
        surfaced as a notify, never a crash.
        """
        pod = self._focused_pod()
        if not pod:
            self.notify("focus a pod row first", severity="warning")
            return
        try:
            with self.suspend():
                subprocess.call(self._shell_cmd(pod))
        except Exception as exc:
            self.notify(f"shell failed: {exc}", severity="error")
            return
        self._request_refresh()

    def action_describe_pod(self) -> None:
        pod = self._focused_pod()
        if pod:
            owner = (f"{pod.owner_kind}/{pod.owner_name}"
                     if pod.owner_kind and pod.owner_name else "")
            self.push_screen(
                DescribeModal(pod.name, pod.namespace, self.context, owner=owner)
            )
        else:
            self.notify("focus a pod row first", severity="warning")

    def action_show_yaml(self) -> None:
        pod = self._focused_pod()
        if pod:
            self.push_screen(YamlViewModal(pod.name, pod.namespace, self.context))
        else:
            self.notify("focus a pod row first", severity="warning")

    def _profile_options_list(self) -> "list[str]":
        """Profile names for the sidebar dropdown: 'generic' first, then the
        discovered profiles, always including the currently active one."""
        try:
            names = list_profiles()
        except Exception:
            names = []
        opts = ["generic"] + [n for n in names if n != "generic"]
        cur = (self.cfg.profile_name or "generic")
        if cur not in opts:
            opts.append(cur)
        return opts

    def set_profile(self, name: str) -> None:
        """Switch the active workload profile live from the sidebar dropdown.

        Profile-authoritative: the chosen profile's ordering, namespaces, optional
        kube context, timezone, thresholds, alertmanager URL, and health probes
        replace the current ones, while the user's session UI prefs (theme, columns, sort,
        panels, name width) are preserved. The switch itself is session-only
        (``persist=False``), so it never silently rewrites the saved config — but
        if "Remember for this context" is enabled, the ``context -> profile`` map
        entry is persisted so the next launch (without ``--profile``) reloads it.
        """
        name = (name or "generic").strip()
        if name == (self.cfg.profile_name or "generic"):
            return
        try:
            new_profile = load_profile(None if name == "generic" else name)
        except Exception as exc:  # unresolved/broken profile -> keep current
            self.notify(f"profile load failed: {exc}", severity="error", timeout=5)
            self._sync_sidebar_state()  # revert the dropdown to the live profile
            return

        # Reassign BEFORE adopting so the 'priority' sort uses the new weights on
        # the very first re-render.
        self.profile = new_profile
        cfg = copy.deepcopy(self.cfg)
        cfg.profile_name = new_profile.name
        cfg.timezone = new_profile.timezone
        cfg.cpu_warn, cfg.cpu_crit = new_profile.cpu_warn, new_profile.cpu_crit
        cfg.mem_warn, cfg.mem_crit = new_profile.mem_warn, new_profile.mem_crit
        cfg.pvc_warn, cfg.pvc_crit = new_profile.pvc_warn, new_profile.pvc_crit
        cfg.alertmanager_url = new_profile.alertmanager_url
        cfg.health_probes = [
            {"name": hp.name, "url": hp.url, "fields": dict(hp.fields)}
            for hp in new_profile.health_probes
        ]
        # The profile's namespaces win. A no-namespace profile (e.g. generic)
        # resets to the default scope rather than lingering on — and persisting —
        # the previous profile's namespaces.
        cfg.namespaces = (list(new_profile.namespaces) if new_profile.namespaces
                          else list(Config().namespaces))
        # A profile may also pin the kube context (cluster). When set, switch to
        # it — _adopt_config rewires the fetcher and refetches. Empty keeps the
        # current context.
        if new_profile.context:
            cfg.context = new_profile.context
        self._adopt_config(cfg, persist=False)
        self._remember_current_profile()
        # A profile switch can change alert/health probes and thresholds even when
        # the namespace set is unchanged, so fetch immediately instead of waiting
        # for the next poll. Bump the scope token so an in-flight fetch made with
        # the OLD probes/ordering can't land as this profile's data, and queue
        # the refresh if one is already running.
        self._bump_fetch_gen()
        self._request_refresh()
        self.notify(f"profile: {new_profile.name}")

    def _context_key(self) -> str:
        """The active kube context, used as the profiles_by_context map key.

        The FULL resolved context name (e.g. an EKS ARN), not the truncated
        sidebar display. Empty when no context is resolved yet (e.g. headless
        tests / kubectl unavailable) — callers skip persistence on an empty key.
        """
        # strip each candidate before the `or` so a blank/whitespace --context
        # falls through to the resolved current-context instead of collapsing to "".
        return (self.context or "").strip() or (self._resolved_context or "").strip()

    def set_context(self, name: str) -> None:
        """Switch the active kube context (cluster) live from the sidebar.

        Mirrors the Options>Cluster picker: adopt the new context (``_adopt_config``
        rewires the fetcher + refetches), then re-discover the new cluster's
        namespaces so the sidebar list matches. Persistence follows the usual rule
        — a generic session keeps it; a profile session resets it on save.
        """
        name = (name or "").strip()
        if name == (self.context or ""):
            return
        cfg = copy.deepcopy(self.cfg)
        cfg.context = name
        self._adopt_config(cfg, persist=True)
        if self._discover_namespaces:
            self.run_worker(self._discover_ns_worker, thread=True,
                            exclusive=False, group="ns")
        self.notify(f"context: {name or 'current'}")

    def _remember_current_profile(self) -> None:
        """Persist the current context -> profile mapping when remembering is on.

        No-op unless ``remember_profile_per_context`` is enabled and a context is
        resolved. Selecting the ``generic`` (no-)profile clears the entry, so the
        map only ever holds real, reloadable profile names.
        """
        if not self.cfg.remember_profile_per_context:
            return
        key = self._context_key()
        if not key:
            return
        name = self.cfg.profile_name or "generic"
        if name == "generic":
            self.cfg.profiles_by_context.pop(key, None)
        else:
            self.cfg.profiles_by_context[key] = name
        self._persist_state()

    def set_remember_profile_per_context(self, value: bool) -> None:
        """Toggle context-keyed profile recall (sidebar 'Remember for this context').

        Turning it on immediately records the active context's current profile;
        turning it off forgets just this context's entry. Either way the flag and
        map are persisted to kutop's config (never the kubeconfig).
        """
        value = bool(value)
        if value == self.cfg.remember_profile_per_context:
            return
        self.cfg.remember_profile_per_context = value
        if value:
            self._remember_current_profile()
        else:
            key = self._context_key()
            if key:
                self.cfg.profiles_by_context.pop(key, None)
        self._persist_state()
        self._sync_sidebar_state()
        ctx = self._context_key() or "this context"
        self.notify(
            f"remembering profile for {ctx}" if value
            else "stopped remembering profile for this context",
            timeout=3,
        )

    def set_allow_destructive(self, value: bool) -> None:
        """Toggle the live destructive-delete gate (sidebar 'Allow delete').

        This is the soft, in-app equivalent of the ``--allow-destructive`` flag
        (which now only seeds the initial state): flipping it on lets the 'x'
        shortcut pop the delete-confirm modal; off makes 'x' a no-op warning.
        Intentionally NOT persisted — it resets each launch so a destructive
        capability is never silently left enabled across sessions.
        """
        value = bool(value)
        if value == self.allow_destructive:
            return
        self.allow_destructive = value
        self._sync_sidebar_state()
        self.notify(
            "delete enabled — focus a pod and press 'x'" if value
            else "delete disabled",
            timeout=3,
        )

    def action_delete_pod(self) -> None:
        # Destructive action safety: gated by the live 'Allow delete' toggle
        # (seeded by --allow-destructive) AND the confirm modal below.
        if not self.allow_destructive:
            self.notify(
                "delete disabled — enable 'Allow delete/restart' in the sidebar "
                "(or launch with --allow-destructive)",
                severity="warning", timeout=4,
            )
            return
        pod = self._focused_pod()
        if not pod:
            self.notify("focus a pod row first", severity="warning")
            return

        def on_confirm(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._do_delete_pod(pod.name, pod.namespace)

        # Full target identity before a destructive action: the cluster context
        # matters as much as the pod when several clusters look alike.
        self.push_screen(
            ConfirmModal(
                "DELETE POD",
                f"context: {self._display_context() or 'current'}\n"
                f"namespace: {pod.namespace}\n"
                f"pod: {pod.name}",
                confirm_label="Delete",
            ),
            on_confirm,
        )

    def _action_failure_detail(self, action: str, stderr: bytes) -> str:
        """Compact kubectl stderr for an action-failure toast.

        200 chars (whitespace collapsed) keeps the actual RBAC/admission/
        webhook reason visible — kubectl errors routinely exceed the old 80.
        The complete stderr also goes to the textual logger for anyone running
        under `textual console` (no toast hint for it: a plain `kutop` launch
        has no devtools, so the log is unreachable for normal users).
        """
        text = stderr.decode(errors="ignore")
        try:
            self.log(f"{action} stderr: {text}")
        except Exception:
            pass  # logging must never mask the real failure
        short = " ".join(text.split())
        return short if len(short) <= 200 else short[:199] + "…"

    def _do_delete_pod(self, name: str, ns: str) -> None:
        async def runner() -> None:
            cmd = ["kubectl"]
            if self.context:
                cmd += ["--context", self.context]
            cmd += ["delete", "pod", name, "-n", ns]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _, err = await proc.communicate()
                if proc.returncode == 0:
                    self.notify(f"deleted {name}")
                else:
                    detail = self._action_failure_detail(f"delete pod {name}", err)
                    self.notify(f"delete failed: {detail}", severity="error")
            except Exception as exc:
                self.notify(f"delete error: {exc}", severity="error")
            self._request_refresh()

        task = asyncio.create_task(runner())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _rollout_target(self, pod: Pod) -> "tuple[Optional[str], str]":
        """Map a pod's controlling owner to a ``kubectl rollout restart`` target.

        Returns ``(target, reason)``: ``target`` is e.g. ``deployment/web``
        (with an empty reason), or ``None`` with a human-readable reason when
        the pod cannot be rolled (bare pod, Job/unknown controller, or an
        underivable Deployment name).

        Deployment-owned pods may surface either as the fetch layer's resolved
        ``Deployment`` owner or as a raw ``ReplicaSet`` ownerReference. A
        ReplicaSet created by a Deployment is named ``<deploy>-<podTemplateHash>``
        (standard, dependency-free naming), so the Deployment is derived by
        stripping the trailing ``-<hash>`` segment — no extra API call — but
        ONLY when that segment actually looks like a pod-template-hash
        (``model.pod_template_hash_like``). A standalone or CRD-managed
        ReplicaSet (e.g. ``web-canary``, an Argo Rollouts RS) is reported as
        un-rollable instead of being mistaken for a Deployment named like its
        prefix.
        """
        kind, name = pod.owner_kind, pod.owner_name
        if not kind or not name:
            return None, "pod has no controller to roll"
        if kind == "ReplicaSet":
            base, _, suffix = name.rpartition("-")
            if base and model.pod_template_hash_like(suffix):
                return f"deployment/{base}", ""
            return None, (f"ReplicaSet '{name}' doesn't look Deployment-managed; "
                          "restart it via its own controller")
        if kind in ("Deployment", "StatefulSet", "DaemonSet"):
            return f"{kind.lower()}/{name}", ""
        return None, f"{kind} pods don't support rollout restart"

    def action_restart_pod(self) -> None:
        # Same live gate as delete: a rollout restart reschedules every pod
        # under the controller, so it must never fire from one stray keypress.
        if not self.allow_destructive:
            self.notify(
                "restart disabled — enable 'Allow delete/restart' in the sidebar "
                "(or launch with --allow-destructive)",
                severity="warning", timeout=4,
            )
            return
        pod = self._focused_pod()
        if not pod:
            self.notify("focus a pod row first", severity="warning")
            return
        target, reason = self._rollout_target(pod)
        if not target:
            self.notify(f"restart unavailable: {reason} — use delete (x) instead",
                        severity="warning", timeout=4)
            return

        def on_confirm(confirmed: Optional[bool]) -> None:
            if confirmed:
                self._do_restart_rollout(target, pod.namespace)

        # Full target identity in the confirm body: cluster context, namespace,
        # the focused pod, and exactly which workload the rollout will restart.
        body = (
            f"context: {self._display_context() or 'current'}\n"
            f"namespace: {pod.namespace}\n"
            f"pod: {pod.name}\n"
            f"restarts: {target}"
        )
        self.push_screen(
            ConfirmModal("RESTART ROLLOUT", body, confirm_label="Restart"),
            on_confirm,
        )

    def _restart_cmd(self, target: str, ns: str) -> "list[str]":
        """argv for ``kubectl rollout restart`` (same --context plumbing as delete)."""
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["rollout", "restart", target, "-n", ns]
        return cmd

    def _do_restart_rollout(self, target: str, ns: str) -> None:
        async def runner() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._restart_cmd(target, ns),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode == 0:
                    self.notify(f"restarted {target}")
                else:
                    detail = self._action_failure_detail(f"restart {target}", err)
                    self.notify(f"restart failed: {detail}", severity="error")
            except Exception as exc:
                self.notify(f"restart error: {exc}", severity="error")
            self._request_refresh()

        task = asyncio.create_task(runner())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ── event panel detail on row select ───────────────────────────────────────
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "events_table":
            return
        kv = str(event.row_key.value or "")
        if not kv.startswith("ev:"):
            return
        try:
            idx = int(kv.split(":", 1)[1])
        except ValueError:
            return
        evs = sorted(self.snapshot.events, key=lambda e: (e.type != "Warning",))
        if 0 <= idx < len(evs):
            ev = evs[idx]
            self.push_screen(EventDetailModal(ev.name, ev.reason, ev.message))
