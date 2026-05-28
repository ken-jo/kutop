"""The kubetop Textual application.

Composes a modern dashboard:
  * SummaryBar (aggregate counters)
  * two TrendGraph sparklines (CPU / MEM overall) — fed by rolling history
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
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
)
from textual.widgets.option_list import Option
from textual.screen import ModalScreen
from textual.worker import Worker

from .. import model
from ..config import (
    Config,
    Profile,
    SORTABLE_KEYS,
    SORT_KEY_TO_COLUMN,
    COLUMN_TO_SORT_KEY,
    build_column_registry,
    load_config,
    save_config,
)
from ..fetch import Fetcher
from ..model import Pod, Snapshot, fmt_age, age_seconds
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

_HISTORY = 120


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
        # In k8s the nodegroup (node.role: eks nodegroup / pool) matters more than
        # the EC2 instance hostname, so lead with it (prominent) and show the
        # short instance name secondary/dim. The hostname's region/domain suffix
        # is dropped (ip-1-2-3-4.<region>.compute.internal -> ip-1-2-3-4).
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
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._ns_options = list(ns_options)
        self._selected = set(selected if selected is not None else ns_options)
        self._show_events = show_events
        self._show_pvc = show_pvc

    def compose(self) -> ComposeResult:
        yield Label("CONTROL PANEL", id="side_title")
        with VerticalScroll(id="side_scroll"):
            yield Label("NAMESPACE  (tick to combine)", classes="side_section")
            with VerticalScroll(id="side_ns_box"):
                yield from self._ns_checkboxes()
            yield Label("PANELS", classes="side_section")
            yield Checkbox("Warning Events", value=self._show_events, id="chk_events")
            yield Checkbox("PVC Storage", value=self._show_pvc, id="chk_pvc")
            yield Label("o: Options/Settings (full config)", id="side_opts_tip")
            yield Label("Tab / b: toggle sidebar", id="side_tip")

    def _ns_checkboxes(self):
        """One Checkbox per known namespace; the namespace is stored in ``name``."""
        for ns in self._ns_options:
            yield Checkbox(ns, value=ns in self._selected, name=ns,
                           classes=self.NS_CLASS)

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

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        app = self.app  # type: ignore[assignment]
        cb = event.checkbox
        if cb.has_class(self.NS_CLASS):
            # a namespace was ticked/unticked -> hand the app the full ticked set
            app.set_namespaces(self.ns_checkbox_state())  # type: ignore[attr-defined]
        elif cb.id == "chk_events":
            app.show_events = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility()  # type: ignore[attr-defined]
        elif cb.id == "chk_pvc":
            app.show_pvc = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility()  # type: ignore[attr-defined]


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

    BINDINGS = [
        ("q", "quit", "Quit"),
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

    def __init__(
        self,
        namespaces: list[str],
        interval: float = 3.0,
        profile: Optional[Profile] = None,
        config: Optional[Config] = None,
        context: Optional[str] = None,
        allow_destructive: bool = False,
        log_tail: int = 150,
        discover_namespaces: bool = True,
        config_path: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.profile = profile or Profile()
        # Unified config: the single source of truth for everything the user can
        # customise. The CLI builds it (defaults->profile->file->CLI). If a caller
        # omits it (legacy callers / snapshot harness), synthesise one from the
        # given namespaces/interval/profile so behaviour is unchanged.
        if config is None:
            config = Config(
                interval=max(1.0, float(interval)),
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

        self.namespaces = list(self.cfg.namespaces)
        self.interval = self.cfg.interval
        self.context = self.cfg.context or None
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
        # Live search term (key '/'); separate from the persisted name_filter so
        # an ad-hoc search never clobbers the saved config. The effective filter
        # is the union of both (see _visible_pods).
        self._search_term = ""
        # Namespaces discovered live on the cluster (for the Options multi-select).
        self._discovered_ns: list[str] = []
        # guarded off for --self-test so the headless smoke test never shells out
        self._discover_namespaces = discover_namespaces
        self._loaded = False

    def _enabled_plugins(self) -> list:
        """Optional plugins enabled for the current config (generic seam).

        Robust: if the plugins package is missing/broken the core keeps running
        with no plugins. The list is recomputed each call so a live config change
        (e.g. health_probes added in the Options modal) re-gates plugins without a
        relaunch.
        """
        try:
            from ..plugins import iter_enabled
        except Exception:
            return []  # no plugins package -> core runs without any plugin
        try:
            return list(iter_enabled(self.cfg))
        except Exception:
            return []

    @staticmethod
    def _probe_specs(cfg: Config) -> list:
        """Translate the config's health_probes dicts into HealthProbe objects.

        The Fetcher only needs ``.name/.url/.fields`` attributes; we reuse the
        :class:`~kubetop.config.HealthProbe` dataclass so the probes module gets the
        attribute access it expects. Empty -> [] (no scraping at all).
        """
        from ..config import HealthProbe
        out = []
        for p in cfg.health_probes or []:
            out.append(HealthProbe(
                name=str(p.get("name", "")),
                url=str(p.get("url", "")),
                fields=dict(p.get("fields", {}) or {}),
            ))
        return out

    # ── compose ──────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SummaryBar(id="summary_bar")            # CRITERIA #5: SummaryBar composed
        with Horizontal(id="trends"):
            yield TrendGraph("CPU OVERALL", "cpu", id="cpu_trend")   # CRITERIA #5: Sparkline trend
            yield TrendGraph("MEM OVERALL", "mem", id="mem_trend")   # CRITERIA #5: Sparkline trend
        with Horizontal(id="main_horizontal"):
            yield SidebarPanel(
                self._sidebar_ns_options(),
                selected=list(self.namespaces),
                show_events=self.show_events,
                show_pvc=self.show_pvc,
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
                    for plugin in self._enabled_plugins():
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
        # Feed rolling history every refresh for the sparklines.
        #
        # BUG FIX #2: never append a spurious 0/None. A partial refresh can
        # produce cap==0 (node fetch failed) or used==0 (metrics-server lag),
        # which would otherwise inject a 0 into an otherwise flat ~55% series
        # and make the Sparkline render as isolated fat blocks with big gaps.
        # When a sample is not trustworthy we repeat the prior value (so the
        # line stays smooth) and only seed an initial value when we have none.
        s = snap.summary
        self._append_trend(self.cpu_hist, s.cpu_used_mcpu, s.cpu_cap_mcpu)
        self._append_trend(self.mem_hist, s.mem_used_mi, s.mem_cap_mi)
        self._render()

    @staticmethod
    def _append_trend(hist: deque[int], used: int, cap: int) -> None:
        """Append a clamped used/cap percent, never letting a dropout 0 in.

        A trustworthy sample requires cap>0 and used>0. Otherwise we carry the
        previous value forward (keeping the trend smooth); if there is no prior
        value we skip seeding a 0 entirely so the series only ever contains
        real, non-zero percentages.
        """
        if cap > 0 and used > 0:
            hist.append(max(0, min(100, model.pct(used, cap))))
        elif hist:
            hist.append(hist[-1])  # carry forward; never inject a 0 dropout
        # else: no prior value and no trustworthy sample -> append nothing

    # ── rendering ──────────────────────────────────────────────────────────────
    def _render(self) -> None:
        snap = self.snapshot
        p = self.profile
        s = snap.summary

        sb = self.query_one("#summary_bar", SummaryBar)
        if sb.style_mode != self.cfg.summary_style:
            sb.set_style_mode(self.cfg.summary_style)
        sb.update_summary(s, show_alerts=bool(p.alertmanager_url))

        cpu_detail = f"{model.fmt_cpu(s.cpu_used_mcpu)}/{model.fmt_cpu(s.cpu_cap_mcpu)}"
        mem_detail = f"{model.fmt_mem(s.mem_used_mi)}/{model.fmt_mem(s.mem_cap_mi)}"
        self.query_one("#cpu_trend", TrendGraph).update_trend(list(self.cpu_hist), cpu_detail)
        self.query_one("#mem_trend", TrendGraph).update_trend(list(self.mem_hist), mem_detail)

        self._render_main_table()
        self._render_events()
        self._render_pvc()
        self._render_alerts()
        self._render_health()

    def _render_alerts(self) -> None:
        try:
            at = self.query_one("#alerts_panel", DataTable)
        except Exception:
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

    def _render_health(self) -> None:
        """Render the health plugin's panel from the snapshot, if it is mounted.

        Generic by contract: the core looks the panel up by its plugin-declared
        ``panel_id`` and calls its ``update_health`` if present. If the health
        plugin is absent the panel was never mounted, so this simply no-ops.
        """
        for plugin in self._enabled_plugins():
            panel_id = getattr(plugin, "panel_id", "")
            if panel_id != "health_panel":
                continue
            try:
                panel = self.query_one(f"#{panel_id}")
                updater = getattr(panel, "update_health", None)
                if callable(updater):
                    updater(list(self.snapshot.health))
            except Exception:
                pass

    def _effective_filter(self) -> str:
        """The active name substring: live search overrides the persisted filter."""
        return (self._search_term or self.cfg.name_filter or "").strip().lower()

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
        # persist the live search term as the saved name_filter so a typed search
        # survives a relaunch (matches the user's "cluster data is adjustable" goal)
        if self._search_term:
            self.cfg.name_filter = self._search_term

    def _persist_state(self) -> None:
        """Persist current config to ~/.config/kubetop/config.yaml. Best effort."""
        self._sync_cfg_from_app()
        try:
            save_config(self.cfg)
        except Exception:
            pass  # PyYAML missing or unwritable path — don't disturb the UI

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
    def apply_config(self, cfg: Config) -> None:
        """Adopt an edited Config: re-render everything live + persist."""
        prev_interval = self.cfg.interval
        prev_ns = list(self.namespaces)
        prev_accent = self.cfg.theme_accent
        prev_alertmgr = self.cfg.alertmanager_url
        prev_probes = list(self.cfg.health_probes)

        self.cfg = cfg
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

        # theme accent change
        if cfg.theme_accent != prev_accent:
            self._apply_accent(cfg.theme_accent)

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

        # interval change -> reset the timer
        if abs(cfg.interval - prev_interval) > 1e-9:
            self.interval = cfg.interval
            try:
                self._refresh_timer.stop()
            except Exception:
                pass
            self._refresh_timer = self.set_interval(self.interval, self.refresh_snapshot)

        self.apply_panel_visibility()
        if self._loaded:
            self._render()

        # namespace change -> refetch + re-sync the sidebar checkboxes so the
        # sidebar (primary control) and the Options modal never contradict.
        if list(cfg.namespaces) != prev_ns:
            self.fetcher.namespaces = list(cfg.namespaces)
            self.fetcher.context = cfg.context or None
            self.context = cfg.context or None
            self._sync_sidebar_ns()
            self.refresh_snapshot()

        try:
            save_config(self.cfg)
        except Exception:
            pass

    def export_config(self, cfg: Optional[Config] = None) -> Optional[str]:
        """Write the full config skeleton to the user file; return its path."""
        target = cfg or self.cfg
        try:
            path = save_config(target)
            self.notify(f"config exported -> {path}")
            return path
        except Exception as exc:
            self.notify(f"export failed: {exc}", severity="error")
            return None

    def _apply_accent(self, accent: str) -> None:
        """Best-effort live theme accent change."""
        try:
            self.theme_variables = {"accent": accent}  # type: ignore[attr-defined]
        except Exception:
            pass

    def action_open_options(self) -> None:
        self._sync_cfg_from_app()
        # hand the modal a copy so a cancelled edit can't half-mutate live state;
        # apply_config() adopts the working copy on every change anyway. The
        # discovered namespace list powers the multi-select.
        import copy
        self.push_screen(
            OptionsModal(copy.deepcopy(self.cfg), discovered_ns=list(self._discovered_ns))
        )

    # ── panel visibility ─────────────────────────────────────────────────────────
    def apply_panel_visibility(self) -> None:
        self.cfg.show_events = self.show_events
        self.cfg.show_pvc = self.show_pvc
        self.cfg.show_alerts = self.show_alerts
        self.cfg.show_health = self.show_health
        self.query_one("#summary_bar").set_class(not self.cfg.show_summary, "-hidden")
        self.query_one("#trends").set_class(not self.cfg.show_trends, "-hidden")
        self.query_one("#main_table").set_class(not self.cfg.show_podtable, "-hidden")
        self.query_one("#events_table").set_class(not self.show_events, "-hidden")
        self.query_one("#pvc_table").set_class(not self.show_pvc, "-hidden")
        self.query_one("#bottom_box").set_class(
            not (self.show_events or self.show_pvc), "-hidden"
        )
        # The alerts panel (generic monitoring, core) is doubly-gated: toggled on
        # AND an alertmanager_url is set.
        alerts_on = self.show_alerts and bool(self.cfg.alertmanager_url)
        self.query_one("#alerts_panel").set_class(not alerts_on, "-hidden")
        # The health panel is owned by the (optional) health plugin. It is gated
        # by the health toggle AND the plugin being enabled for the current
        # config. If the plugin is absent its panel was never mounted, so the
        # query is best-effort. ``any_plugin_on`` keeps the top row open when any
        # plugin panel is showing.
        health_on = self.show_health and self._plugin_panel_visible("health_panel")
        any_plugin_on = self._apply_plugin_panel_visibility()
        # collapse the whole top row when no panel is shown (no empty band)
        self.query_one("#top_panels").set_class(
            not (alerts_on or any_plugin_on), "-hidden"
        )
        self._persist_state()

    def _plugin_panel_visible(self, panel_id: str) -> bool:
        """Whether the plugin behind ``panel_id`` is enabled (and thus mounted)."""
        for plugin in self._enabled_plugins():
            if getattr(plugin, "panel_id", "") == panel_id:
                return True
        return False

    def _apply_plugin_panel_visibility(self) -> bool:
        """Show/hide each enabled plugin's panel; return True if any is visible.

        Generic: the core toggles a plugin panel using only its declared
        ``panel_id`` plus the matching panel toggle. The health plugin maps to the
        ``show_health`` toggle; other plugins default to shown when enabled.
        """
        any_on = False
        for plugin in self._enabled_plugins():
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
        self.apply_panel_visibility()

    def action_toggle_pvc(self) -> None:
        self.show_pvc = not self.show_pvc
        try:
            self.query_one("#chk_pvc", Checkbox).value = self.show_pvc
        except Exception:
            pass
        self.apply_panel_visibility()

    def action_toggle_alerts(self) -> None:
        self.show_alerts = not self.show_alerts
        if self.show_alerts and not self.cfg.alertmanager_url:
            self.notify("alerts: set probes.alertmanager_url to enable",
                        severity="warning")
        self.apply_panel_visibility()

    def action_toggle_health(self) -> None:
        self.show_health = not self.show_health
        if self.show_health and not self.cfg.health_probes:
            self.notify("health: add probes.health_probes to enable",
                        severity="warning")
        self.apply_panel_visibility()

    def action_reload_config(self) -> None:
        """Re-read the user config file and apply it live (M5 hot-reload).

        Re-runs the same layered load the CLI used (defaults -> profile -> file)
        so an edit to ``~/.config/kubetop/config.yaml`` takes effect without a
        restart. Robust: a missing/broken file falls back to defaults+profile and
        the app keeps running. Shows a toast either way.
        """
        try:
            cfg = load_config(profile=self.profile, user_path=self._config_path)
        except Exception as exc:
            self.notify(f"reload failed: {exc}", severity="error")
            return
        self.apply_config(cfg)
        src = self._config_path or "~/.config/kubetop/config.yaml"
        self.notify(f"config reloaded from {src}")

    def action_toggle_sidebar(self) -> None:
        sb = self.query_one("#sidebar")
        hidden = not sb.has_class("-hidden")
        sb.set_class(hidden, "-hidden")

    def action_refresh(self) -> None:
        self.refresh_snapshot()
        self.notify("refreshing...")

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
        if self._loaded:
            self._render_main_table()

    # ── search / filter (key '/') ───────────────────────────────────────────────
    def action_search(self) -> None:
        """Reveal the search bar and focus its input."""
        bar = self.query_one("#search_bar", SearchBar)
        bar.set_class(False, "-hidden")
        bar.set_value(self._search_term)
        bar.focus_input()

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

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter the pod table as the user types in the search bar."""
        if event.input.id == "search_input":
            self._search_term = event.value
            if self._loaded:
                self._render_main_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            # keep the filter applied; move focus back to the table
            try:
                self.query_one("#main_table", DataTable).focus()
            except Exception:
                pass

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
        self.notify(f"namespaces: {', '.join(ns_list)}")
        self._persist_state()
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

    def action_delete_pod(self) -> None:
        # Destructive action safety: gated by --allow-destructive + confirm modal.
        if not self.allow_destructive:
            self.notify(
                "destructive actions disabled (run with --allow-destructive)",
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
