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
import os
import subprocess
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import Reactive
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)
from textual.widget import Widget
from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderTitle
from textual.screen import ModalScreen

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
from .widgets import (
    _severity_style,
    OptionsModal,
    ThemeMenuModal,
    SearchBar,
    SummaryBar,
    TrendGraph,
    ConfirmModal,
    bar_gauge,
    level_color,
)

_HISTORY = 120
_HIDDEN_THEMES = {"ansi-dark", "ansi-light"}


_BINDING_SPECS = [
    ("q", "quit_hint", "Quit?"),
    ("r", "refresh", "Refresh"),
    ("o", "open_options", "Options"),
    ("slash", "search", "Search"),
    ("escape", "clear_search", "Clear"),
    ("tab", "toggle_sidebar", "Sidebar"),
    ("b", "toggle_sidebar", "Sidebar"),
    ("s", "cycle_sort", "Sort"),
    ("S", "toggle_sort_dir", "SortDir"),
    ("g", "toggle_group", "Group"),
    ("l", "show_logs", "Logs"),
    ("d", "describe_pod", "Describe"),
    ("x", "delete_pod", "Delete"),
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


class ThemeHeaderIcon(Widget):
    """Header icon that opens kutop's menu instead of Textual's palette."""

    DEFAULT_CSS = """
    ThemeHeaderIcon {
        dock: left;
        padding: 0 1;
        width: 8;
        content-align: left middle;
    }

    ThemeHeaderIcon:hover {
        background: $foreground 10%;
    }
    """

    icon = Reactive("☰")

    def on_mount(self) -> None:
        self.tooltip = "Open kutop menu"

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app.action_open_theme_menu()  # type: ignore[attr-defined]

    def render(self) -> str:
        return str(self.icon)


class MetricsIndicator(Static):
    """Top-right metrics-freshness readout (read-only).

    The refresh cadence is fixed, so this slot no longer hosts a +/- control.
    Instead it exposes how fresh the CPU/MEM numbers are: `kubectl top` reads
    metrics-server, whose scrape resolution is METRICS_RESOLUTION_SECS, so the
    metric values only move that often regardless of the poll cadence. Docked to
    the right of the header clock; the app sets its text via
    :meth:`TopApp._update_metrics_indicator`.
    """

    DEFAULT_CSS = """
    MetricsIndicator {
        dock: right;
        width: auto;
        padding: 0 1;
        /* clear the 10-wide header clock zone so we sit to its left */
        margin-right: 10;
    }
    """


class ThemeHeader(Header):
    """Header whose hamburger icon opens kutop's theme menu."""

    def compose(self) -> ComposeResult:
        yield ThemeHeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        # Yielded before the clock: among right-docked widgets the earlier one
        # sits further left, so this lands to the clock's left, like btop.
        yield MetricsIndicator(id="metrics_indicator")
        yield (
            HeaderClock().data_bind(Header.time_format)
            if self._show_clock
            else HeaderClockSpace()
        )

    def _on_click(self) -> None:
        self.app.action_open_theme_menu()  # type: ignore[attr-defined]


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


# ── interactive modals (ported from top_v2.py) ───────────────────────────────


class LogViewerModal(ModalScreen):
    """Asynchronous live log streaming (`kubectl logs -f`)."""

    def __init__(self, pod_name: str, ns: str, tail: int, context: Optional[str]) -> None:
        super().__init__()
        self.pod_name = pod_name
        self.ns = ns
        self.tail = tail
        self.context = context
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.log_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="log_box"):
            yield Label(f"Live Logs: {self.pod_name} [{self.ns}] — ESC/q to close", id="log_hdr")
            yield RichLog(id="log_content", highlight=True, max_lines=2000)

    async def on_mount(self) -> None:
        self.log_task = asyncio.create_task(self._stream())

    async def _stream(self) -> None:
        log = self.query_one("#log_content", RichLog)
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["logs", "-n", self.ns, self.pod_name, "-f", f"--tail={self.tail}"]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert self.proc.stdout is not None
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                log.write(line.decode("utf-8", errors="ignore").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.write(f"[error] {exc}")

    async def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            await self._close()

    async def _close(self) -> None:
        if self.log_task:
            self.log_task.cancel()
        if self.proc:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except Exception:
                pass
        self.dismiss()


class DescribeModal(ModalScreen):
    """`kubectl describe pod` viewer."""

    def __init__(self, pod_name: str, ns: str, context: Optional[str],
                 owner: str = "") -> None:
        super().__init__()
        self.pod_name = pod_name
        self.ns = ns
        self.context = context
        # e.g. "StatefulSet/<name>" — surfaced in the header when known.
        self.owner = owner

    def compose(self) -> ComposeResult:
        owner_suffix = f" ({self.owner})" if self.owner else ""
        with Vertical(id="desc_box"):
            yield Label(
                f"Describe: {self.pod_name}{owner_suffix} [{self.ns}] — ESC/q to close",
                id="desc_hdr",
            )
            yield RichLog(id="desc_content", highlight=True)

    async def on_mount(self) -> None:
        log = self.query_one("#desc_content", RichLog)
        log.write("Loading kubectl describe...")
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["describe", "pod", self.pod_name, "-n", self.ns]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            log.clear()
            if out:
                log.write(out.decode("utf-8", errors="ignore"))
            if err:
                log.write(f"\n[stderr]\n{err.decode('utf-8', errors='ignore')}")
        except Exception as exc:
            log.write(f"[error] {exc}")

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            self.dismiss()


class EventDetailModal(ModalScreen):
    """Full event metadata dialog."""

    def __init__(self, name: str, reason: str, message: str) -> None:
        super().__init__()
        self._name = name
        self._reason = reason
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="ev_box"):
            yield Label("Event detail — ESC/q to close", id="ev_hdr")
            yield RichLog(id="ev_content")

    def on_mount(self) -> None:
        log = self.query_one("#ev_content", RichLog)
        log.write(Text.from_markup(f"[bold yellow]Object:[/]  {self._name}"))
        log.write(Text.from_markup(f"[bold yellow]Reason:[/]  {self._reason}"))
        log.write(Text.from_markup(f"[bold yellow]Message:[/]\n{self._message}"))

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            self.dismiss()


# ── sidebar ───────────────────────────────────────────────────────────────────


class SidebarPanel(Vertical):
    """Collapsible control sidebar: ns checkboxes, sort mode, panel toggles.

    The NAMESPACE section renders one :class:`Checkbox` per known namespace; the
    user ticks any combination and the dashboard shows the pods of every ticked
    namespace COMBINED (the Fetcher already accepts and merges a namespace list).
    The namespace checkboxes are the primary selector — the Options modal mirrors
    the same ticked set via ``apply_config``.

    BUG FIX #3: the controls live inside a ``VerticalScroll`` so that on short
    terminals the lower sections (NAMESPACE / PANELS) remain reachable by
    scrolling instead of being clipped off the bottom of the viewport. The
    namespace checkbox list itself sits in its own bounded ``VerticalScroll`` so
    that a cluster with many namespaces never overflows the lower controls.
    """

    #: each namespace checkbox carries this class so the change handler can tell
    #: them apart from the panel-toggle checkboxes (which have stable ids).
    NS_CLASS = "ns-checkbox"

    def __init__(
        self,
        ns_options: list[str],
        selected: "Optional[list[str]]" = None,
        show_events: bool = True,
        show_pvc: bool = True,
        show_summary: bool = True,
        show_trends: bool = True,
        show_alerts: bool = True,
        show_health: bool = True,
        show_keys: bool = True,
        sort_key: str = "priority",
        sort_desc: bool = False,
        group_by_node: bool = False,
        allow_delete: bool = False,
        profile_name: str = "generic",
        profile_options: "Optional[list[str]]" = None,
        remember_profile: bool = False,
        interval: float = REFRESH_INTERVAL_SECS,
        context: Optional[str] = None,
        name_filter: str = "",
        key_context: str = "DASHBOARD",
        key_rows: "Optional[list[tuple[str, str]]]" = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._ns_options = list(ns_options)
        self._selected = set(selected if selected is not None else ns_options)
        self._show_events = show_events
        self._show_pvc = show_pvc
        self._show_summary = show_summary
        self._show_trends = show_trends
        self._show_alerts = show_alerts
        self._show_health = show_health
        self._show_keys = show_keys
        self._sort_key = sort_key if sort_key in SORTABLE_KEYS else "priority"
        self._sort_desc = sort_desc
        self._group_by_node = group_by_node
        self._allow_delete = allow_delete
        self._profile_name = profile_name or "generic"
        self._profile_options = list(profile_options or [])
        self._remember_profile = remember_profile
        self._interval = interval
        self._context_name = context or ""
        self._name_filter = name_filter
        self._key_context = key_context
        self._key_rows = list(key_rows or [])
        self._syncing = False
        self._ready_for_input = False

    def compose(self) -> ComposeResult:
        yield Static("", id="side_status")
        with VerticalScroll(id="side_scroll"):
            yield Label("PROFILE", classes="side_section")
            yield Select(
                [(p, p) for p in self._profile_options] or [("generic", "generic")],
                value=self._profile_name,
                id="side_profile",
                allow_blank=False,
            )
            yield Checkbox("Remember for this context", value=self._remember_profile,
                           id="chk_remember_profile", compact=True)
            yield Label("NAMESPACES", classes="side_section side_section_spaced")
            with VerticalScroll(id="side_ns_box"):
                yield from self._ns_checkboxes()
            yield Label("SORT", classes="side_section side_section_spaced")
            yield Select(
                [(k, k) for k in SORTABLE_KEYS],
                value=self._sort_key,
                id="side_sort",
                allow_blank=False,
            )
            yield Checkbox("Descending", value=self._sort_desc, id="chk_sort_desc",
                           compact=True)
            yield Checkbox("Group by node", value=self._group_by_node, id="chk_group",
                           compact=True)
            yield Label("PANELS", classes="side_section side_section_spaced")
            yield Checkbox("Summary", value=self._show_summary, id="chk_summary",
                           compact=True)
            yield Checkbox("Trends", value=self._show_trends, id="chk_trends",
                           compact=True)
            yield Checkbox("Warning Events", value=self._show_events, id="chk_events",
                           compact=True)
            yield Checkbox("PVC Storage", value=self._show_pvc, id="chk_pvc",
                           compact=True)
            yield Checkbox("Alerts", value=self._show_alerts, id="chk_alerts",
                           compact=True)
            yield Checkbox("Health", value=self._show_health, id="chk_health",
                           compact=True)
            yield Checkbox("Keys", value=self._show_keys, id="chk_keys",
                           compact=True)
            yield Label("ACTIONS", classes="side_section side_section_spaced")
            yield Checkbox("Allow delete (x)", value=self._allow_delete,
                           id="chk_allow_delete", compact=True)
        with Vertical(id="side_keys_box"):
            yield Label(
                "KEYS",
                classes="side_section",
                id="side_keys_title",
            )
            yield Static("", id="side_keys_body")

    def on_mount(self) -> None:
        self.border_title = "SIDEBAR"
        self.update_state(
            selected=list(self._selected),
            show_events=self._show_events,
            show_pvc=self._show_pvc,
            show_summary=self._show_summary,
            show_trends=self._show_trends,
            show_alerts=self._show_alerts,
            show_health=self._show_health,
            show_keys=self._show_keys,
            sort_key=self._sort_key,
            sort_desc=self._sort_desc,
            group_by_node=self._group_by_node,
            allow_delete=self._allow_delete,
            profile_name=self._profile_name,
            remember_profile=self._remember_profile,
            interval=self._interval,
            context=self._context_name,
            name_filter=self._name_filter,
            key_context=self._key_context,
            key_rows=self._key_rows,
        )
        try:
            self.call_after_refresh(self._enable_input_events)
        except Exception:
            self._ready_for_input = True

    def _enable_input_events(self) -> None:
        self._ready_for_input = True

    def _ns_checkboxes(self):
        """One Checkbox per known namespace; the namespace is stored in ``name``."""
        for ns in self._ns_options:
            yield Checkbox(ns, value=ns in self._selected, name=ns,
                           classes=self.NS_CLASS, compact=True)

    def rebuild_namespaces(self, ns_options: list[str], selected: list[str]) -> None:
        """Repopulate the namespace checkbox list (live discovery / config sync).

        Mounts a fresh Checkbox per namespace inside ``#side_ns_box`` reflecting
        the given ticked ``selected`` set. Best effort: silently no-ops if the
        container is not mounted yet (first synchronous compose handles that).
        """
        self._ns_options = list(ns_options)
        self._selected = set(selected)
        try:
            box = self.query_one("#side_ns_box", VerticalScroll)
        except Exception:
            return
        for existing in list(box.query(Checkbox)):
            existing.remove()
        box.mount(*self._ns_checkboxes())

    def ns_checkbox_state(self) -> "list[str]":
        """The currently-ticked namespaces, in display order."""
        out: list[str] = []
        try:
            for cb in self.query_one("#side_ns_box", VerticalScroll).query(Checkbox):
                if cb.value and cb.name:
                    out.append(cb.name)
        except Exception:
            pass
        return out

    def update_state(
        self,
        *,
        selected: list[str],
        show_events: bool,
        show_pvc: bool,
        show_summary: bool,
        show_trends: bool,
        show_alerts: bool,
        show_health: bool,
        show_keys: bool,
        sort_key: str,
        sort_desc: bool,
        group_by_node: bool,
        allow_delete: bool,
        profile_name: str,
        remember_profile: bool,
        interval: float,
        context: str,
        name_filter: str,
        key_context: str,
        key_rows: list[tuple[str, str]],
    ) -> None:
        """Refresh compact status text and control values from the app state."""
        self._selected = set(selected)
        self._show_events = show_events
        self._show_pvc = show_pvc
        self._show_summary = show_summary
        self._show_trends = show_trends
        self._show_alerts = show_alerts
        self._show_health = show_health
        self._show_keys = show_keys
        self._sort_key = sort_key if sort_key in SORTABLE_KEYS else "priority"
        self._sort_desc = sort_desc
        self._group_by_node = group_by_node
        self._allow_delete = allow_delete
        self._profile_name = profile_name or "generic"
        self._remember_profile = remember_profile
        self._interval = interval
        self._context_name = context or ""
        self._name_filter = name_filter
        self._key_context = key_context or "DASHBOARD"
        self._key_rows = list(key_rows or [])
        try:
            ns_count = len([n for n in selected if n])
            ctx = self._context_name or "current"
            # long contexts (e.g. EKS ARNs) hold the useful name at the end —
            # show the last path segment, then tail-truncate, not the prefix
            if len(ctx) > 24:
                ctx = ctx.rsplit("/", 1)[-1] if "/" in ctx else ctx
                if len(ctx) > 24:
                    ctx = "…" + ctx[-23:]
            direction = "desc" if self._sort_desc else "asc"
            status = Text()
            status.append("ns=", style="dim")
            status.append(str(ns_count), style="bold green")
            status.append(" | refresh=", style="dim")
            status.append(f"{self._interval:g}s", style="bold cyan")
            status.append("\nctx=", style="dim")
            status.append(ctx[:24], style="bold")
            status.append("\nsort=", style="dim")
            status.append(self._sort_key, style="bold magenta")
            status.append(" | dir=", style="dim")
            status.append(direction, style="bold yellow")
            # only show the filter line when a filter is active, so the common
            # case stays 3 lines and leaves more room for the panel toggles
            if self._name_filter:
                status.append("\nfilter=", style="dim")
                status.append(self._name_filter[:22], style="bold")
            self.query_one("#side_status", Static).update(status)
        except Exception:
            pass
        self._syncing = True
        try:
            self._set_checkbox("chk_summary", show_summary)
            self._set_checkbox("chk_trends", show_trends)
            self._set_checkbox("chk_events", show_events)
            self._set_checkbox("chk_pvc", show_pvc)
            self._set_checkbox("chk_alerts", show_alerts)
            self._set_checkbox("chk_health", show_health)
            self._set_checkbox("chk_keys", show_keys)
            self._set_checkbox("chk_sort_desc", sort_desc)
            self._set_checkbox("chk_group", group_by_node)
            self._set_checkbox("chk_allow_delete", allow_delete)
            self._set_checkbox("chk_remember_profile", remember_profile)
            try:
                self.query_one("#side_sort", Select).value = self._sort_key
            except Exception:
                pass
            try:
                self.query_one("#side_profile", Select).value = self._profile_name
            except Exception:
                pass
            self._render_keys_panel()
        finally:
            self._syncing = False

    def _render_keys_panel(self) -> None:
        box = self.query_one("#side_keys_box", Vertical)
        title = self.query_one("#side_keys_title", Label)
        body = self.query_one("#side_keys_body", Static)
        box.set_class(not self._show_keys, "-hidden")
        title.set_class(not self._show_keys, "-hidden")
        body.set_class(not self._show_keys, "-hidden")
        if not self._show_keys:
            return
        title.update(f"KEYS · {self._key_context}")
        if not self._key_rows:
            body.update("focus pod for keys")
            return
        text = Text()
        for index, (key, label) in enumerate(self._key_rows):
            if index:
                text.append("\n")
            text.append(f"{key:<5}", style="bold cyan")
            text.append(label)
        body.update(text)

    def _set_checkbox(self, widget_id: str, value: bool) -> None:
        try:
            cb = self.query_one(f"#{widget_id}", Checkbox)
            if cb.value != value:
                cb.value = value
        except Exception:
            pass

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._syncing or not self._ready_for_input:
            return
        app = self.app  # type: ignore[assignment]
        cb = event.checkbox
        if cb.has_class(self.NS_CLASS):
            # a namespace was ticked/unticked -> hand the app the full ticked set
            app.set_namespaces(self.ns_checkbox_state())  # type: ignore[attr-defined]
        elif cb.id == "chk_summary":
            app.cfg.show_summary = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_trends":
            app.cfg.show_trends = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_events":
            app.show_events = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_pvc":
            app.show_pvc = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_alerts":
            app.show_alerts = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_health":
            app.show_health = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_keys":
            app.cfg.show_keys = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_sort_desc":
            app.cfg.sort_desc = event.value  # type: ignore[attr-defined]
            app._persist_state()  # type: ignore[attr-defined]
            app._restamp_sort_header()  # type: ignore[attr-defined]
            if app._loaded:  # type: ignore[attr-defined]
                app._render_main_table()  # type: ignore[attr-defined]
        elif cb.id == "chk_group":
            app.cfg.group_by_node = event.value  # type: ignore[attr-defined]
            app._persist_state()  # type: ignore[attr-defined]
            if app._loaded:  # type: ignore[attr-defined]
                app._render_main_table()  # type: ignore[attr-defined]
        elif cb.id == "chk_allow_delete":
            app.set_allow_destructive(event.value)  # type: ignore[attr-defined]
        elif cb.id == "chk_remember_profile":
            app.set_remember_profile_per_context(event.value)  # type: ignore[attr-defined]

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._syncing or not self._ready_for_input:
            return
        if event.select.id == "side_sort" and event.value is not Select.BLANK:
            self.app.set_sort_key(str(event.value))  # type: ignore[attr-defined]
        elif event.select.id == "side_profile" and event.value is not Select.BLANK:
            self.app.set_profile(str(event.value))  # type: ignore[attr-defined]


# ── resizable main table ───────────────────────────────────────────────────────


class ResizableDataTable(DataTable):
    """A ``DataTable`` whose NODE/POD (first) column is mouse-drag resizable.

    The user grabs the column's RIGHT EDGE and drags to grow/shrink it live; on
    release the new width is written to ``app.cfg.name_width`` and persisted so
    it survives a relaunch. Everything else (header-click sort, row cursor,
    vertical/horizontal scroll, scroll-preservation) behaves exactly as the base
    ``DataTable`` — only a click that lands on the resize column's right boundary
    starts a drag; all other clicks fall straight through to the base handlers.

    Hit-testing is done in TABLE CONTENT coordinates: ``_get_column_region`` gives
    the column's region (already accounting for the row-label column + the prior
    columns + cell padding), and we convert the incoming widget-relative mouse x
    to content-x by adding ``scroll_x``. A click within ``_GRAB`` cells of the
    column's right boundary begins the resize.
    """

    #: index of the column whose right edge is draggable (NODE/POD = first).
    RESIZE_COLUMN_INDEX = 0
    #: how close (cells) to the boundary a click must land to grab it.
    _GRAB = 1

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._resizing = False
        # content-x of the column's LEFT edge captured at drag start (so the new
        # width = mouse_content_x - left, independent of scroll during the drag).
        self._resize_left = 0

    # ── geometry helpers ────────────────────────────────────────────────────
    def _content_x(self, event_x: int) -> int:
        """Convert a widget-relative mouse x to table CONTENT x (adds scroll_x)."""
        return int(event_x) + int(self.scroll_x)

    def _resize_boundary_x(self) -> Optional[int]:
        """Content-x of the resize column's RIGHT edge (None if unavailable)."""
        try:
            region = self._get_column_region(self.RESIZE_COLUMN_INDEX)
        except Exception:
            return None
        if region.width <= 0:
            return None
        return region.right

    def _on_resize_boundary(self, event_x: int) -> bool:
        """True if a widget-x click lands within ``_GRAB`` of the right boundary."""
        boundary = self._resize_boundary_x()
        if boundary is None:
            return False
        return abs(self._content_x(event_x) - boundary) <= self._GRAB

    # ── mouse drag (public handlers; base core handlers are _on_mouse_*) ─────
    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Begin a resize drag only when the click is on the column boundary.

        Otherwise return without stopping the event so the base DataTable still
        gets it (cursor move / header sort dispatch happen on the base handler).
        """
        if not self._on_resize_boundary(event.x):
            return  # not the boundary -> let DataTable handle normally
        region = self._get_column_region(self.RESIZE_COLUMN_INDEX)
        self._resize_left = region.x
        self._resizing = True
        try:
            self.capture_mouse()
        except Exception:
            pass
        event.stop()
        event.prevent_default()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._resizing:
            return  # not dragging -> base handles hover/scroll as usual
        from ..config import clamp_name_width
        # new render-region width = mouse content-x - column left edge; subtract
        # the cell padding the region adds on both sides to recover the content
        # width that Config.name_width represents.
        raw = self._content_x(event.x) - self._resize_left - 2 * self.cell_padding
        new_width = clamp_name_width(raw)
        self._set_name_width_live(new_width)
        event.stop()
        event.prevent_default()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._resizing:
            return
        self._resizing = False
        try:
            self.release_mouse()
        except Exception:
            pass
        # commit the final width to the app config + persist (survives relaunch)
        app = self.app
        if app is not None and hasattr(app, "commit_name_width"):
            try:
                col = self.ordered_columns[self.RESIZE_COLUMN_INDEX]
                app.commit_name_width(int(col.width))  # type: ignore[attr-defined]
            except Exception:
                pass
        event.stop()
        event.prevent_default()

    def _set_name_width_live(self, width: int) -> None:
        """Apply ``width`` to the resize column in-place and repaint (no persist)."""
        try:
            col = self.ordered_columns[self.RESIZE_COLUMN_INDEX]
        except (IndexError, AttributeError):
            return
        col.width = int(width)
        col.auto_width = False
        # mirror onto the live config so the next _render's cell-fit uses it
        app = self.app
        if app is not None and hasattr(app, "cfg"):
            try:
                app.cfg.name_width = int(width)  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            self._update_column_widths(set())
        except Exception:
            pass
        self.refresh()


# ── main app ───────────────────────────────────────────────────────────────────


class TopApp(App):
    CSS_PATH = os.path.join(os.path.dirname(__file__), "theme.tcss")
    TITLE = f"kutop v{__version__}"

    BINDINGS = [
        *_BINDING_SPECS,
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
        self.theme = self._coerce_theme(self.cfg.theme)
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

        # Mirror panel/sort state onto the app for the existing toggle paths.
        self.sort_mode = self.cfg.sort_mode
        self.show_events = self.cfg.show_events
        self.show_pvc = self.cfg.show_pvc
        self.show_alerts = self.cfg.show_alerts
        self.show_health = self.cfg.show_health
        # Live search term (key '/'). Config.name_filter is only an initial
        # CLI --filter seed; it is cleared before any save so ad-hoc searches
        # cannot survive a relaunch.
        self._search_term = (self.cfg.name_filter or "").strip()
        self.cfg.name_filter = ""
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
                profile_options=self._profile_opts,
                remember_profile=self.cfg.remember_profile_per_context,
                interval=self.interval,
                context=self._display_context(),
                name_filter=self._effective_filter(),
                key_context="DASHBOARD",
                key_rows=[],
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
        pt.add_columns("PVC", "STORAGE (USE/CAP)", "GAUGE", "PHASE")

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
        if discovered:
            self.call_from_thread(self._populate_ns_list, discovered)

    def _populate_ns_list(self, discovered: list[str]) -> None:
        """Rebuild the sidebar NAMESPACE checkboxes from live discovery.

        Keeps the currently-ticked set ticked (and ensures every selected
        namespace stays present even if discovery somehow omits it).
        """
        # remember the full cluster ns list for the Options multi-select
        self._discovered_ns = list(discovered)
        try:
            sidebar = self.query_one("#sidebar", SidebarPanel)
        except Exception:
            return
        sidebar.rebuild_namespaces(
            self._sidebar_ns_options(discovered), list(self.namespaces)
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
        """
        if getattr(self, "_fetching", False):
            return
        self._fetching = True
        self.run_worker(
            self._fetch_worker, thread=True, exclusive=True, group="fetch"
        )

    def _fetch_worker(self) -> None:
        try:
            if not self._loaded:
                snap = self.fetcher.fetch_core()
                try:
                    self.call_from_thread(self._apply_snapshot, snap)
                except Exception:
                    pass
                snap = self.fetcher.enrich_snapshot(snap)
            else:
                snap = self.fetcher.fetch()
            # marshal back to the UI thread; if the app is tearing down the call
            # may be rejected — swallow it so the worker thread returns cleanly.
            try:
                self.call_from_thread(self._apply_snapshot, snap)
            except Exception:
                pass
        finally:
            self._fetching = False

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

    def _apply_snapshot(self, snap: Snapshot) -> None:
        if snap.error and not snap.nodes and not snap.pods:
            # full failure: keep previous frame, surface error
            self.notify(f"refresh failed: {snap.error}", severity="error", timeout=4)
            return
        self.snapshot = snap
        self._loaded = True
        # Feed rolling history every refresh for the trend meters. A zero used
        # value with a real capacity is a real 0% sample; only missing capacity
        # is treated as an untrustworthy dropout.
        s = snap.summary
        self._append_trend(self.cpu_hist, s.cpu_used_mcpu, s.cpu_cap_mcpu)
        self._append_trend(self.mem_hist, s.mem_used_mi, s.mem_cap_mi)
        self._render()

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
        """The active runtime name substring."""
        return (self._search_term or "").strip().lower()

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
        sub = self._effective_filter()
        out = []
        for pd in pods:
            if sub and sub not in pd.name.lower():
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
            msg = "(no pods match filter)" if self._effective_filter() or \
                self.cfg.only_problems else "(no pods)"
            mt.add_row(*sep_row(msg))

        self._restore_row(mt, saved_key, sx, sy)
        self._sync_sidebar_state()

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
        """Fold the app's live mirror fields back into self.cfg before saving."""
        self.cfg.sort_mode = self.sort_mode
        self.cfg.show_events = self.show_events
        self.cfg.show_pvc = self.show_pvc
        self.cfg.show_alerts = self.show_alerts
        self.cfg.show_health = self.show_health
        self.cfg.namespaces = list(self.namespaces)
        self.cfg.name_filter = ""

    def _persist_state(self) -> None:
        """Persist current config to the active config file (--config or default).

        Best effort — failure must never disturb the UI.
        """
        self._sync_cfg_from_app()
        try:
            save_config(self.cfg, self._config_path)
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
        # mirror onto the app fields the legacy toggle/sort paths still use
        self.sort_mode = cfg.sort_mode
        self.show_events = cfg.show_events
        self.show_pvc = cfg.show_pvc
        self.show_alerts = cfg.show_alerts
        self.show_health = cfg.show_health
        self.namespaces = list(cfg.namespaces)
        self.tz = _resolve_tz(cfg.timezone)

        # probe (re)wiring: if the alertmanager URL or health probes changed,
        # update the fetcher so the next refresh picks them up live.
        if cfg.alertmanager_url != prev_alertmgr or cfg.health_probes != prev_probes:
            self.fetcher.alertmanager_url = cfg.alertmanager_url
            self.fetcher.health_probes = self._probe_specs(cfg)

        # summary style change -> reflow the header tiles
        sb = self.query_one("#summary_bar", SummaryBar)
        if sb.style_mode != cfg.summary_style:
            sb.set_style_mode(cfg.summary_style)

        # rebuild columns if the visible set/order OR the sort indicator changed.
        # Compare against the table's ACTUAL header labels (which now carry the
        # ▲/▼ sort glyph) so a sort_key/sort_desc change re-stamps the header.
        mt = self.query_one("#main_table", DataTable)
        want = [self._column_label(k) for k in cfg.visible_columns()]
        have = [col.label.plain for col in mt.ordered_columns]
        if want != have:
            self._build_main_columns(mt)
        else:
            # columns unchanged but the adopted config may carry a different
            # name_width (e.g. hot-reload 'R' or an edited config) — re-pin it.
            idx = self._name_column_index(mt)
            if idx is not None:
                self._apply_name_width(mt, idx)

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
            self.fetcher.namespaces = list(cfg.namespaces)
            self.fetcher.context = cfg.context or None
            self.context = cfg.context or None
            if list(cfg.namespaces) != prev_ns:
                self._sync_sidebar_ns()
            self.refresh_snapshot()

        if persist:
            try:
                save_config(self.cfg, self._config_path)
            except Exception:
                pass

    def apply_config(self, cfg: Config) -> None:
        """Adopt an edited Config: re-render everything live + persist."""
        self._adopt_config(cfg, persist=True)

    def export_config(self, cfg: Optional[Config] = None) -> Optional[str]:
        """Write the full config skeleton to the user file; return its path."""
        target = cfg or self.cfg
        try:
            path = save_config(target, self._config_path)
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
            save_config(self.cfg, self._config_path)
        except Exception:
            pass

    def action_open_theme_menu(self) -> None:
        self.push_screen(ThemeMenuModal())

    def action_open_options(self) -> None:
        self._sync_cfg_from_app()
        # hand the modal a copy so a cancelled edit can't half-mutate live state;
        # apply_config() adopts the working copy on every change anyway. The
        # discovered namespace list powers the multi-select.
        import copy
        self.push_screen(
            OptionsModal(
                copy.deepcopy(self.cfg),
                discovered_ns=list(self._discovered_ns),
                context_names=self._context_options(),
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
        self.cfg.show_events = self.show_events
        self.cfg.show_pvc = self.show_pvc
        self.cfg.show_alerts = self.show_alerts
        self.cfg.show_health = self.show_health
        self.cfg.show_keys = bool(self.cfg.show_keys)
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
            return (
                "SEARCH",
                [
                    (_binding_key("search"), "Edit search"),
                    ("enter", "Keep filter"),
                    (_binding_key("clear_search"), "Clear"),
                ],
            )

        pod = self._focused_pod()
        if pod:
            rows = [
                (_binding_key("show_logs"), "Logs"),
                (_binding_key("describe_pod"), "Describe"),
            ]
            delete_label = "Delete" if self.allow_destructive else "Delete disabled"
            rows.append((_binding_key("delete_pod"), delete_label))
            return ("POD ROW", rows)

        return ("DASHBOARD", [])

    def _sync_sidebar_state(self) -> None:
        """Mirror app state into the sidebar controls without rebuilding rows."""
        try:
            sidebar = self.query_one("#sidebar", SidebarPanel)
        except Exception:
            return
        key_context, key_rows = self._sidebar_key_context()
        sidebar.update_state(
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
            key_rows=key_rows,
        )

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
        src = self._config_path or "~/.config/kutop/config.yaml"
        self.notify(f"config reloaded from {src}")

    def action_toggle_sidebar(self) -> None:
        sb = self.query_one("#sidebar")
        hidden = not sb.has_class("-hidden")
        sb.set_class(hidden, "-hidden")

    def action_refresh(self) -> None:
        self.refresh_snapshot()
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
        # keep the legacy mirror coherent for any old code path
        self.sort_mode = key if key in ("priority", "cpu", "mem", "name") else "priority"
        self.cfg.sort_mode = self.sort_mode
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
        """Reveal the search bar and focus its input."""
        bar = self.query_one("#search_bar", SearchBar)
        bar.set_class(False, "-hidden")
        bar.set_value(self._search_term)
        bar.focus_input()
        self._sync_sidebar_state()

    def action_clear_search(self) -> None:
        """Clear the live search term and hide the bar."""
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
        if getattr(event.data_table, "id", "") == "main_table":
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
        self.cfg.namespaces = ns_list
        self.fetcher.namespaces = ns_list
        self._reset_trend_history()
        self.notify(f"namespaces: {', '.join(ns_list)}")
        self._persist_state()
        self._sync_sidebar_state()
        self.refresh_snapshot()

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
            self.push_screen(LogViewerModal(pod.name, pod.namespace, self.log_tail, self.context))
        else:
            self.notify("focus a pod row first", severity="warning")

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

        Profile-authoritative: the chosen profile's ordering, namespaces,
        timezone, thresholds, alertmanager URL, and health probes replace the
        current ones, while the user's session UI prefs (theme, columns, sort,
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

        import copy

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
        self._adopt_config(cfg, persist=False)
        self._remember_current_profile()
        # A profile switch can change alert/health probes and thresholds even when
        # the namespace set is unchanged, so fetch immediately instead of waiting
        # for the next poll. No-op if a refresh is already in flight (e.g. when
        # _adopt_config already triggered one for a namespace change).
        self.refresh_snapshot()
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
                "delete disabled — enable 'Allow delete' in the sidebar "
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

        self.push_screen(
            ConfirmModal(
                "DELETE POD",
                f"Delete pod {pod.name} in {pod.namespace}?",
                confirm_label="Delete",
            ),
            on_confirm,
        )

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
                    self.notify(f"delete failed: {err.decode(errors='ignore')[:80]}",
                                severity="error")
            except Exception as exc:
                self.notify(f"delete error: {exc}", severity="error")
            self.refresh_snapshot()

        asyncio.create_task(runner())

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
