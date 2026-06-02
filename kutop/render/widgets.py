"""Presentation widgets for kutop.

These widgets hold no fetching logic and no workload knowledge. The app feeds
them already-computed data (a Snapshot, history deques, threshold-resolved
colors). Styling lives in ``theme.tcss``; only data-driven Rich markup is built
here.
"""

from __future__ import annotations

from statistics import mean
from typing import Optional

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    Select,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from ..config import (
    Config,
    _VALID_ACCENTS,
    _VALID_SUMMARY_STYLES,
    SORTABLE_KEYS,
    _DEFAULT_COLUMN_ORDER,
    build_column_registry,
)
from ..model import Summary

# ── threshold color helper ───────────────────────────────────────────────────


def level_color(value: Optional[int], warn: int, crit: int) -> str:
    """Map a percentage to a semantic color class name (used in markup)."""
    if value is None:
        return "dim"
    if value >= crit:
        return "red"
    if value >= warn:
        return "yellow"
    return "green"


# Fine eighth-block glyphs for sub-cell precision (▏▎▍▌▋▊▉█). Index by eighths.
_EIGHTHS = " ▏▎▍▌▋▊▉█"


def bar_gauge(value: Optional[int], warn: int, crit: int, width: int = 10) -> Text:
    """A proportional bar gauge colored by threshold.

    Fills ``width`` cells with full blocks plus a fractional leading eighth-block
    so the bar length is proportional to ``value`` at sub-cell resolution (rather
    than the old coarse round-to-cell that produced sparse/short bars). The
    unfilled remainder is a dim track. ``None`` -> dim placeholder so unknown
    usage is visually distinct from 0%.
    """
    if value is None:
        return Text("·" * width, style="grey37")
    v = max(0, min(100, value))
    color = level_color(v, warn, crit)
    # total eighths to fill across the whole bar
    total_eighths = int(round(v * width * 8 / 100))
    full = total_eighths // 8
    rem = total_eighths % 8
    bar = Text()
    if full:
        bar.append("█" * full, style=color)
    track = width - full
    if rem and track > 0:
        bar.append(_EIGHTHS[rem], style=color)
        track -= 1
    if track > 0:
        bar.append("░" * track, style="grey37")
    return bar


# ── dual-handle threshold slider (Thresholds tab) ─────────────────────────────


class DualThresholdSlider(Static):
    """A 0–100 track with TWO draggable handles: ``warn`` (lower) + ``crit`` (upper).

    Replaces the pair of numeric warn/crit Inputs for one metric (CPU / MEM /
    PVC). The user drags either handle with the mouse; keyboard users move the
    active handle with ``left``/``right`` and switch handles with ``[`` / ``]``
    (or ``space``). The track is colored by zone — 0..warn green, warn..crit
    amber, crit..100 red — and each handle's numeric value sits under it.

    On every change the widget posts :class:`ThresholdChanged(metric, warn, crit)`
    which the Options modal forwards into ``cfg`` so the live table gauges
    re-color immediately, and the config persists. The handles are always
    clamped so ``warn <= crit`` with at least a 1-point gap.

    Pure presentation + input: it owns no fetching or workload knowledge.
    """

    can_focus = True

    # Fine eighth-block fill so the colored zones land at sub-cell resolution.
    _EIGHTHS = " ▏▎▍▌▋▊▉█"
    _MIN_GAP = 1

    class ThresholdChanged(Message):
        """Posted whenever a handle moves. Carries the resolved warn/crit pair."""

        def __init__(self, metric: str, warn: int, crit: int) -> None:
            super().__init__()
            self.metric = metric
            self.warn = warn
            self.crit = crit

    def __init__(self, metric: str, warn: int, crit: int, label: str = "",
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.metric = metric
        self._label = label or metric.upper()
        self._warn = self._clamp(warn)
        self._crit = self._clamp(crit)
        self._enforce_gap(prefer="crit")          # ensure warn <= crit at start
        # which handle the keyboard / a fresh drag acts on ("warn" | "crit")
        self._active = "warn"
        # set True between mouse_down and mouse_up while a handle is grabbed
        self._dragging = False

    # ── value helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _clamp(v) -> int:
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            v = 0
        return max(0, min(100, v))

    @property
    def warn(self) -> int:
        return self._warn

    @property
    def crit(self) -> int:
        return self._crit

    def set_values(self, warn: int, crit: int) -> None:
        """Programmatically set both handles (used to sync from the config)."""
        self._warn = self._clamp(warn)
        self._crit = self._clamp(crit)
        self._enforce_gap(prefer="crit")
        self._redraw()

    def _enforce_gap(self, prefer: str) -> None:
        """Clamp so ``warn <= crit`` with a >= ``_MIN_GAP`` separation.

        ``prefer`` names the handle that just moved and should keep its value;
        the other handle is pushed to respect the gap (and the overall 0..100
        bounds). Guarantees the lower handle can never pass the upper one.
        """
        gap = self._MIN_GAP
        if self._warn > self._crit - gap:
            if prefer == "warn":
                # warn moved up: push crit up (or pull warn back if at the top)
                self._crit = min(100, self._warn + gap)
                if self._crit - gap < 0:
                    self._crit = gap
                self._warn = min(self._warn, self._crit - gap)
            else:
                # crit moved down: push warn down (or pull crit back at the floor)
                self._warn = max(0, self._crit - gap)
                self._crit = max(self._crit, self._warn + gap)
        self._warn = max(0, min(100 - gap, self._warn))
        self._crit = max(gap, min(100, self._crit))

    # ── geometry: track width <-> value mapping ─────────────────────────────
    def _track_width(self) -> int:
        """Number of cells the 0..100 track spans (>= 1).

        Uses the widget's content width when mounted; falls back to a sane
        default so headless construction (tests / first paint) still maps x.
        """
        w = 0
        try:
            w = int(self.content_size.width)
        except Exception:
            w = 0
        if w <= 0:
            w = 40
        return max(1, w)

    def value_at_x(self, x: int, width: "Optional[int]" = None) -> int:
        """Map a content-x cell (0-based) to a 0..100 value across the track.

        The left edge (x=0) is 0; the right edge (x=width-1) is 100. Out-of-range
        x clamps to the nearest end. ``width`` is injectable for tests so the
        mapping can be exercised headlessly without a real layout.
        """
        w = width if width is not None else self._track_width()
        if w <= 1:
            return 0
        frac = x / (w - 1)
        return self._clamp(round(frac * 100))

    def _value_to_x(self, value: int, width: int) -> int:
        if width <= 1:
            return 0
        return int(round(self._clamp(value) / 100 * (width - 1)))

    def _nearest_handle(self, value: int) -> str:
        """Return the handle ("warn"/"crit") whose value is closest to ``value``."""
        if abs(value - self._warn) <= abs(value - self._crit):
            return "warn"
        return "crit"

    def _set_handle(self, handle: str, value: int) -> None:
        """Set one handle's value, re-clamp the gap, redraw + notify on change."""
        value = self._clamp(value)
        before = (self._warn, self._crit)
        if handle == "warn":
            self._warn = value
        else:
            self._crit = value
        self._enforce_gap(prefer=handle)
        self._redraw()
        if (self._warn, self._crit) != before:
            self._notify_change()

    def _notify_change(self) -> None:
        """Post a ThresholdChanged message. No-op when not mounted (tests)."""
        try:
            self.post_message(
                self.ThresholdChanged(self.metric, self._warn, self._crit)
            )
        except Exception:
            pass

    # ── mouse drag ─────────────────────────────────────────────────────────
    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.focus()
        width = self._track_width()
        value = self.value_at_x(int(event.offset.x), width)
        self._active = self._nearest_handle(value)
        self._dragging = True
        try:
            self.capture_mouse()
        except Exception:
            pass
        self._set_handle(self._active, value)
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        width = self._track_width()
        value = self.value_at_x(int(event.offset.x), width)
        self._set_handle(self._active, value)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        self._dragging = False
        try:
            self.release_mouse()
        except Exception:
            pass
        # final persist of the resolved pair (idempotent if unchanged)
        self._notify_change()
        event.stop()

    # ── keyboard fallback (no-mouse terminals) ──────────────────────────────
    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("left", "h"):
            cur = self._warn if self._active == "warn" else self._crit
            self._set_handle(self._active, cur - 1)
            event.stop()
        elif key in ("right", "l"):
            cur = self._warn if self._active == "warn" else self._crit
            self._set_handle(self._active, cur + 1)
            event.stop()
        elif key in ("left_square_bracket", "right_square_bracket", "space"):
            # switch which handle the keyboard controls
            self._active = "crit" if self._active == "warn" else "warn"
            self._redraw()
            event.stop()

    # ── rendering ────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw()

    def _redraw(self) -> None:
        # update() needs a live app/console; when unmounted (headless logic
        # tests) we silently skip the repaint — the values are still correct.
        try:
            self.update(self.render_slider())
        except Exception:
            pass

    def render_slider(self, width: "Optional[int]" = None) -> Text:
        """Build the colored track + handle labels as Rich Text.

        Line 1: ``LABEL`` title with the live warn/crit numbers.
        Line 2: the 0..100 colored track with ▲ handle markers.
        Line 3: numeric value labels positioned under each handle.
        """
        w = width if width is not None else self._track_width()
        warn_x = self._value_to_x(self._warn, w)
        crit_x = self._value_to_x(self._crit, w)

        # ── title line ──────────────────────────────────────────────────
        title = Text()
        title.append(f"{self._label}  ", style="bold")
        warn_sty = "bold yellow" + (" reverse" if self._active == "warn" else "")
        crit_sty = "bold red" + (" reverse" if self._active == "crit" else "")
        title.append("warn ", style="dim")
        title.append(f"{self._warn}%", style=warn_sty)
        title.append("  crit ", style="dim")
        title.append(f"{self._crit}%", style=crit_sty)

        # ── track line: per-cell zone coloring (green/amber/red) ─────────
        track = Text()
        for x in range(w):
            val = self.value_at_x(x, w)
            if x == warn_x:
                track.append("▲", style="bold yellow")
            elif x == crit_x:
                track.append("▲", style="bold red")
            elif val < self._warn:
                track.append("━", style="green")
            elif val < self._crit:
                track.append("━", style="yellow")
            else:
                track.append("━", style="red")

        # ── label line: numeric values under their handles ───────────────
        labels = self._handle_label_line(w, warn_x, crit_x)

        out = Text()
        out.append_text(title)
        out.append("\n")
        out.append_text(track)
        out.append("\n")
        out.append_text(labels)
        return out

    def _handle_label_line(self, w: int, warn_x: int, crit_x: int) -> Text:
        """A line of spaces with each handle's value printed under its marker."""
        cells = [(" ", "") for _ in range(max(w, 1))]

        def place(pos: int, text: str, style: str) -> None:
            # center the label under the handle marker, clamped into the track
            start = max(0, min(w - len(text), pos - len(text) // 2))
            for i, ch in enumerate(text):
                idx = start + i
                if 0 <= idx < len(cells):
                    cells[idx] = (ch, style)

        # draw crit first so a colliding warn label wins the overlap cells
        place(crit_x, f"{self._crit}", "bold red")
        place(warn_x, f"{self._warn}", "bold yellow")

        line = Text()
        for ch, style in cells:
            line.append(ch, style=style or None)
        return line


# ── common titled-bordered-scrollable panel base ──────────────────────────────


class Panel(VerticalScroll):
    """One reusable titled, bordered, scrollable side-panel.

    The one common spec every kutop side panel shares so they stop looking
    "each different": a rounded border, a left-aligned accent ``border_title``,
    consistent padding, and a scrollable body. The body is a single
    :class:`Static`; :meth:`set_body` swaps its renderable and :meth:`set_title`
    updates the border title. Because the panel is a ``VerticalScroll`` the body
    scrolls vertically (mouse wheel + keyboard) whenever it overflows the panel's
    fixed height — no truncation.

    All panels carry the ``kpanel`` CSS class so ``theme.tcss`` applies identical
    chrome (border-title alignment/color, scrollbar styling). Subclasses set their
    own border *color* (a semantic accent) via an id rule, but the style/title/
    padding stay uniform across every panel.

    Pure presentation: it holds no fetching logic and no workload knowledge.
    """

    DEFAULT_CLASSES = "kpanel"

    def __init__(self, title: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._body: "Optional[Static]" = None
        #: last renderable handed to :meth:`set_body` (kept so callers/tests can
        #: introspect the rendered content without reaching into Static internals).
        self._last_renderable: RenderableType = ""

    def compose(self) -> ComposeResult:
        self._body = Static(id=None, classes="kpanel-body")
        yield self._body

    def on_mount(self) -> None:
        # set the border title on the panel (rendered on the top border line)
        if self._title:
            self.border_title = self._title

    def set_title(self, title: str) -> None:
        self._title = title
        try:
            self.border_title = title
        except Exception:
            pass

    def set_body(self, renderable: RenderableType) -> None:
        """Replace the scrollable body content (Rich Text/markup/etc.)."""
        self._last_renderable = renderable
        try:
            body = self._body or self.query_one(".kpanel-body", Static)
            body.update(renderable)
        except Exception:
            pass

    def body_text(self) -> str:
        """Plain text of the last body renderable (introspection / tests)."""
        r = self._last_renderable
        if hasattr(r, "plain"):
            return r.plain  # rich Text
        return str(r)


# ── summary counter bar ───────────────────────────────────────────────────────


class SummaryBar(Static):
    """Top-of-screen aggregate counters for fast situational awareness.

    Two layouts, chosen by ``Config.summary_style``:

    * ``tiles``   — a row of distinct, boxed, color-coded stat tiles for
      5-second awareness (Nodes / Pods R·P·F / Restarts / OOM / Warnings /
      CPU% / MEM%). Severity drives each tile's color. Compact in height.
    * ``compact`` — the original one-line separated counter bar (revert path).

    The app sets ``style_mode`` + the ``-tiles`` CSS class before calling
    :meth:`update_summary`, so theme.tcss can size the widget per mode.
    """

    style_mode: str = "tiles"

    def set_style_mode(self, mode: str) -> None:
        self.style_mode = mode if mode in ("tiles", "compact") else "tiles"
        self.set_class(self.style_mode == "tiles", "-tiles")

    def update_summary(self, s: Summary, show_alerts: bool) -> None:
        if self.style_mode == "compact":
            self.update(self._compact(s, show_alerts))
        else:
            self.update(self._tiles(s, show_alerts))

    # ── tile layout ───────────────────────────────────────────────────────
    @staticmethod
    def _tile(label: str, value: Text, accent: str) -> Text:
        """One boxed stat tile: a top label line over a bold value line."""
        box = Text()
        box.append(f"┤ {label} ├", style=f"bold {accent}")
        box.append("\n")
        box.append(value)
        return box

    def _tiles(self, s: Summary, show_alerts: bool) -> Text:
        nodes_ok = s.nodes_ready == s.nodes_total
        nodes_accent = "green" if nodes_ok else "yellow"
        nodes_val = Text(f"{s.nodes_ready}/{s.nodes_total}",
                         style=f"bold {nodes_accent}")

        pods_val = Text()
        pods_val.append(str(s.pods_running), style="bold green")
        pods_val.append(" · ", style="grey37")
        pods_val.append(str(s.pods_pending),
                        style="bold yellow" if s.pods_pending else "bold green")
        pods_val.append(" · ", style="grey37")
        pods_val.append(str(s.pods_failed),
                        style="bold red" if s.pods_failed else "bold green")

        rst_accent = "red" if s.restarts_total else "green"
        oom_accent = "red" if s.oomkilled_total else "green"
        warn_accent = "yellow" if s.warn_events else "green"

        cpu_pct = (s.cpu_used_mcpu * 100 // s.cpu_cap_mcpu) if s.cpu_cap_mcpu else 0
        mem_pct = (s.mem_used_mi * 100 // s.mem_cap_mi) if s.mem_cap_mi else 0
        cpu_accent = "red" if cpu_pct >= 90 else "yellow" if cpu_pct >= 75 else "green"
        mem_accent = "red" if mem_pct >= 92 else "yellow" if mem_pct >= 80 else "green"

        tiles = [
            self._tile("NODES", nodes_val, nodes_accent),
            self._tile("PODS R·P·F", pods_val, "cyan"),
            self._tile("RESTARTS", Text(str(s.restarts_total),
                       style=f"bold {rst_accent}"), rst_accent),
            self._tile("OOM", Text(str(s.oomkilled_total),
                       style=f"bold {oom_accent}"), oom_accent),
            self._tile("WARN", Text(str(s.warn_events),
                       style=f"bold {warn_accent}"), warn_accent),
            self._tile("CPU", Text(f"{cpu_pct}%", style=f"bold {cpu_accent}"),
                       cpu_accent),
            self._tile("MEM", Text(f"{mem_pct}%", style=f"bold {mem_accent}"),
                       mem_accent),
        ]
        if show_alerts:
            al_accent = "red" if s.alerts_firing else "green"
            tiles.append(self._tile("ALERTS", Text(str(s.alerts_firing),
                         style=f"bold {al_accent}"), al_accent))

        # Join the per-tile two-line blocks side by side with a separator.
        return self._join_blocks(tiles, sep="  ")

    @staticmethod
    def _join_blocks(blocks: "list[Text]", sep: str = "  ") -> Text:
        """Place multi-line Text blocks side by side, padding each to a tile.

        Each block is two lines (label / value); we widen each line to the
        block's max width so the columns stay aligned, then concatenate with a
        separator between tiles.
        """
        split = []
        for blk in blocks:
            lines = blk.split("\n")
            while len(lines) < 2:
                lines.append(Text(""))
            width = max(line.cell_len for line in lines) + 1
            split.append((lines, width))

        out = Text()
        for row in range(2):
            if row:
                out.append("\n")
            for i, (lines, width) in enumerate(split):
                if i:
                    out.append(sep)
                cell = lines[row].copy()
                pad = width - cell.cell_len
                if pad > 0:
                    cell.append(" " * pad)
                out.append_text(cell)
        return out

    # ── compact (legacy) layout ───────────────────────────────────────────
    def _compact(self, s: Summary, show_alerts: bool) -> Text:
        t = Text()

        def seg(label: str, value: str, style: str = "bold") -> None:
            t.append(f" {label} ", style="bold dim")
            t.append(value, style=style)
            t.append("  │ ", style="grey37")

        nodes_style = "bold green" if s.nodes_ready == s.nodes_total else "bold yellow"
        seg("NODES", f"{s.nodes_ready}/{s.nodes_total}", nodes_style)

        pods_txt = Text()
        pods_txt.append(str(s.pods_running), style="bold green")
        pods_txt.append("/")
        pods_txt.append(str(s.pods_pending), style="bold yellow")
        pods_txt.append("/")
        pods_txt.append(str(s.pods_failed), style="bold red")
        t.append(" PODS(R/P/F) ", style="bold dim")
        t.append_text(pods_txt)
        t.append("  │ ", style="grey37")

        seg("RESTARTS", str(s.restarts_total),
            "bold red" if s.restarts_total else "bold green")
        seg("OOM", str(s.oomkilled_total),
            "bold red" if s.oomkilled_total else "bold green")
        seg("WARN", str(s.warn_events),
            "bold yellow" if s.warn_events else "bold green")

        cpu_pct = (s.cpu_used_mcpu * 100 // s.cpu_cap_mcpu) if s.cpu_cap_mcpu else 0
        mem_pct = (s.mem_used_mi * 100 // s.mem_cap_mi) if s.mem_cap_mi else 0
        seg("CPU", f"{cpu_pct}%", "bold")
        seg("MEM", f"{mem_pct}%", "bold")

        if show_alerts:
            t.append(" ALERTS ", style="bold dim")
            t.append(str(s.alerts_firing),
                     style="bold red" if s.alerts_firing else "bold green")

        return t


# ── alerts panel helpers (M2) + health row (M3) ───────────────────────────────


_SEVERITY_STYLE = {
    "critical": "bold red",
    "error": "bold red",
    "warning": "bold yellow",
    "warn": "bold yellow",
    "info": "cyan",
    "none": "green",
}


def _severity_style(severity: str) -> str:
    return _SEVERITY_STYLE.get((severity or "").lower(), "white")


# ── search / filter bar (key '/') ─────────────────────────────────────────────


class SearchBar(Horizontal):
    """A one-line live pod-name filter, shown/hidden with the ``/`` key.

    Hidden by default. ``/`` reveals it and focuses the Input; typing filters
    the pod table live (case-insensitive substring); ``Esc`` clears the filter
    and hides the bar. The app owns the wiring — this widget only emits the
    standard ``Input.Changed`` / ``Input.Submitted`` messages.
    """

    def compose(self) -> ComposeResult:
        yield Label("/", id="search_glyph")
        yield Input(placeholder="filter pods by name (Esc clears)…",
                    id="search_input")

    def focus_input(self) -> None:
        try:
            self.query_one("#search_input", Input).focus()
        except Exception:
            pass

    @property
    def value(self) -> str:
        try:
            return self.query_one("#search_input", Input).value
        except Exception:
            return ""

    def set_value(self, val: str) -> None:
        try:
            self.query_one("#search_input", Input).value = val
        except Exception:
            pass


# ── trend graph (Sparkline + live label) ──────────────────────────────────────


class TrendGraph(Vertical):
    """A labeled live sparkline (CPU or MEM overall trend).

    Wraps Textual's ``Sparkline`` widget (available in 8.2.7) fed by a rolling
    history of percentages. Shows the current value alongside the trend.

    BUG FIX #2 (chunky/gappy MEM sparkline): the Sparkline auto-scales between
    the min and max of its data, so a single spurious 0 in an otherwise flat
    ~55% series collapses every real sample into the top bucket and renders the
    line as isolated fat blocks with gaps. We fix this two ways:

      * the app never appends 0/None samples (see ``app._apply_snapshot``), and
      * we render on a *fixed* 0..100 scale by anchoring the data so a flat
        ~55% series shows as a smooth, consistent band rather than bimodal
        blocks. Both CPU and MEM graphs use the same ``summary_function`` (mean)
        for identical visual behaviour.
    """

    # render against a fixed full-range scale so flat series look smooth and
    # CPU/MEM are visually comparable (not each auto-scaled to its own range)
    _SCALE_MIN = 0
    _SCALE_MAX = 100

    def __init__(self, title: str, accent: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._accent = accent

    def compose(self) -> ComposeResult:
        # title sits on the border line (border_title, set on_mount) like the
        # data panels; the body is the sparkline + the summary value line.
        yield Sparkline([0], summary_function=mean, classes="trend-spark")
        yield Label("--", classes="trend-value")

    def on_mount(self) -> None:
        self.border_title = self._title
        self.query_one(Sparkline).add_class(self._accent)

    def update_trend(self, history: list[int], detail: str) -> None:
        spark = self.query_one(Sparkline)
        # Clamp every sample to [0, 100] and anchor the series to the fixed
        # scale so the Sparkline's internal min/max never collapses a flat
        # series into bimodal blocks. The leading anchors fall off the left as
        # real history fills the rolling window.
        clamped = [max(self._SCALE_MIN, min(self._SCALE_MAX, int(v))) for v in history]
        if clamped:
            data = [self._SCALE_MIN, self._SCALE_MAX, *clamped]
        else:
            data = [self._SCALE_MIN, self._SCALE_MAX]
        spark.data = data
        cur = clamped[-1] if clamped else 0
        self.query_one(".trend-value", Label).update(f"{cur}%  {detail}")


# ── confirm modal for destructive actions ─────────────────────────────────────


class ConfirmModal(ModalScreen[bool]):
    """Generic yes/no confirmation. Dismisses with True (confirm) or False."""

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, body: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_box"):
            yield Label(self._title, id="confirm_title")
            yield Label(self._body, id="confirm_body")
            with Horizontal(id="confirm_btns"):
                yield Button(f"{self._confirm_label} (y)", variant="error", id="confirm_yes")
                yield Button("Cancel (n)", variant="default", id="confirm_no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm_yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── Options / Settings modal — the visible config "skeleton" ──────────────────


class OptionsModal(ModalScreen):
    """The full configurable-option skeleton: every setting, current value, live.

    Organised into topic TABS (``TabbedContent`` / ``TabPane``): View, Columns,
    Panels, Thresholds, Cluster, Profile. Each pane scrolls internally so no tab
    overflows on a short (~80x24) terminal; the modal header and the footer action
    row (Export / Close) stay OUTSIDE the tabs and are always visible. Editing a
    control applies immediately (via the app's ``apply_config`` callbacks) and
    persists to ``~/.config/kutop/config.yaml``. An "Export config" button writes
    the complete config and reports the path.

    The app passes its live :class:`Config`; this modal mutates a working copy
    and calls back into the app so the dashboard re-renders without a relaunch.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("o", "close", "Close"),
    ]

    def __init__(self, cfg: Config, discovered_ns: "Optional[list[str]]" = None) -> None:
        super().__init__()
        self._cfg = cfg
        self._reg = build_column_registry()
        self._committed_accent = cfg.theme_accent
        self._accent_preview_dirty = False
        # All namespaces we know about for the multi-select: the live cluster
        # discovery (if any), unioned with whatever is currently selected so a
        # config-only namespace is never dropped from the list.
        ns_all = list(discovered_ns or [])
        for ns in cfg.namespaces:
            if ns not in ns_all:
                ns_all.append(ns)
        self._ns_all = ns_all

    # ── compose ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        c = self._cfg
        with Vertical(id="opt_box"):
            yield Label("OPTIONS / SETTINGS  —  the full config skeleton (ESC/o to close)",
                        id="opt_hdr")
            with TabbedContent(id="opt_tabs"):
                # View ──────────────────────────────────────────────────────
                with TabPane("View", id="opt_tab_view"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Input(value=str(c.interval), placeholder="seconds",
                                    id="opt_interval", classes="opt_input")
                        yield Label("  interval (refresh seconds, min 1.0)",
                                    classes="opt_hint")
                        yield Select(
                            [(m, m) for m in SORTABLE_KEYS],
                            value=c.sort_key, id="opt_sort", allow_blank=False,
                        )
                        yield Label("  sort_key (sort by any column; 's' cycles in-app)",
                                    classes="opt_hint")
                        yield Checkbox("Sort descending (▼)", value=c.sort_desc,
                                       id="opt_sort_desc")
                        yield Select(
                            [(a, a) for a in _VALID_ACCENTS],
                            value=c.theme_accent, id="opt_accent", allow_blank=False,
                        )
                        yield Label("  theme_accent", classes="opt_hint")
                        yield Select(
                            [(s, s) for s in _VALID_SUMMARY_STYLES],
                            value=c.summary_style, id="opt_summary_style",
                            allow_blank=False,
                        )
                        yield Label("  summary_style (top header layout)",
                                    classes="opt_hint")
                        yield Checkbox("Group pods by node (topology)",
                                       value=c.group_by_node, id="opt_group_by_node")

                        # Filters live alongside View (which pods the table shows)
                        yield Label("FILTERS", classes="opt_section")
                        yield Input(value=c.name_filter, id="opt_name_filter",
                                    classes="opt_input", placeholder="name substring")
                        yield Label("  name_filter (case-insensitive substring)",
                                    classes="opt_hint")
                        yield Checkbox("Hide completed (Succeeded/Completed) pods",
                                       value=c.hide_completed, id="opt_hide_completed")
                        yield Checkbox("Only problems (non-Running / restarts>0 / oom)",
                                       value=c.only_problems, id="opt_only_problems")

                # Columns ───────────────────────────────────────────────────
                with TabPane("Columns", id="opt_tab_columns"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label("Toggle visible; ↑/↓ + [ / ] to reorder",
                                    classes="opt_hint")
                        yield OptionList(id="opt_columns")
                        with Horizontal(classes="opt_btnrow"):
                            yield Button("Toggle (space)", id="opt_col_toggle",
                                         classes="opt_btn")
                            yield Button("Up [", id="opt_col_up", classes="opt_btn")
                            yield Button("Down ]", id="opt_col_down", classes="opt_btn")

                # Panels ─────────────────────────────────────────────────────
                with TabPane("Panels", id="opt_tab_panels"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Checkbox("Summary bar", value=c.show_summary,
                                       id="opt_p_summary")
                        yield Checkbox("Trend graphs", value=c.show_trends,
                                       id="opt_p_trends")
                        yield Checkbox("Pod table", value=c.show_podtable,
                                       id="opt_p_podtable")
                        yield Checkbox("Warning events", value=c.show_events,
                                       id="opt_p_events")
                        yield Checkbox("PVC storage", value=c.show_pvc, id="opt_p_pvc")
                        yield Checkbox("Alerts (AlertManager)", value=c.show_alerts,
                                       id="opt_p_alerts")
                        yield Checkbox("Health row (workload probes)",
                                       value=c.show_health, id="opt_p_health")

                # Thresholds ─────────────────────────────────────────────────
                with TabPane("Thresholds", id="opt_tab_thresholds"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label(
                            "Drag the two handles (warn / crit). Keyboard: "
                            "←/→ move, [ / ] switch handle.",
                            classes="opt_hint",
                        )
                        yield DualThresholdSlider(
                            "cpu", c.cpu_warn, c.cpu_crit, label="CPU",
                            id="opt_slider_cpu", classes="opt_slider",
                        )
                        yield DualThresholdSlider(
                            "mem", c.mem_warn, c.mem_crit, label="MEM",
                            id="opt_slider_mem", classes="opt_slider",
                        )
                        yield DualThresholdSlider(
                            "pvc", c.pvc_warn, c.pvc_crit, label="PVC (per-pod storage)",
                            id="opt_slider_pvc", classes="opt_slider",
                        )

                # Cluster ────────────────────────────────────────────────────
                with TabPane("Cluster", id="opt_tab_cluster"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label("  namespaces (space toggles; multi-select)",
                                    classes="opt_hint")
                        yield OptionList(id="opt_namespaces")
                        yield Input(value=c.context, id="opt_context",
                                    classes="opt_input", placeholder="kube context")
                        yield Label("  context (kubeconfig context; blank = current)",
                                    classes="opt_hint")

                # Profile (read-only) + cluster-linked probes ────────────────
                with TabPane("Profile", id="opt_tab_profile"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label(f"  active profile: {c.profile_name}  (read-only)",
                                    classes="opt_hint", id="opt_profile_lbl")
                        yield Label("PROBES  (cluster-linked; opt-in)",
                                    classes="opt_section")
                        yield Input(value=c.alertmanager_url, id="opt_alertmanager_url",
                                    classes="opt_input",
                                    placeholder="http://…/api/v2/alerts")
                        yield Label("  alertmanager_url (blank = alerts panel hidden)",
                                    classes="opt_hint")
                        hp_n = len(c.health_probes)
                        yield Label(
                            f"  health_probes: {hp_n} configured "
                            "(edit in config.yaml / profile)",
                            classes="opt_hint", id="opt_health_probes_lbl",
                        )

            with Horizontal(id="opt_footer"):
                yield Button("Export config", id="opt_export", variant="primary")
                yield Button("Close", id="opt_close", variant="default")
            yield Label("", id="opt_status")

    def on_mount(self) -> None:
        self._rebuild_columns()
        self._rebuild_namespaces()

    # ── namespace multi-select ────────────────────────────────────────────
    def _rebuild_namespaces(self) -> None:
        ol = self.query_one("#opt_namespaces", OptionList)
        sel = ol.highlighted
        ol.clear_options()
        chosen = set(self._cfg.namespaces)
        for ns in self._ns_all:
            on = ns in chosen
            mark = "[green]●[/]" if on else "[grey37]○[/]"
            ol.add_option(Option(f"{mark} {ns}", id=f"ns::{ns}"))
        if sel is not None and sel < ol.option_count:
            ol.highlighted = sel

    def _toggle_ns(self) -> None:
        ol = self.query_one("#opt_namespaces", OptionList)
        if ol.highlighted is None:
            return
        opt = ol.get_option_at_index(ol.highlighted)
        oid = opt.id or ""
        if not oid.startswith("ns::"):
            return
        ns = oid.split("::", 1)[1]
        chosen = list(self._cfg.namespaces)
        if ns in chosen:
            if len(chosen) > 1:           # keep at least one namespace
                chosen.remove(ns)
        else:
            chosen.append(ns)
        self._cfg.namespaces = chosen or ["default"]
        self._rebuild_namespaces()
        self._apply()

    # ── columns list (visible + hidden, ordered) ──────────────────────────
    def _rebuild_columns(self) -> None:
        ol = self.query_one("#opt_columns", OptionList)
        sel = ol.highlighted
        ol.clear_options()
        visible = self._cfg.visible_columns()
        # visible (in order) first, then remaining hidden columns
        ordered = list(visible) + [k for k in _DEFAULT_COLUMN_ORDER if k not in visible]
        for key in ordered:
            spec = self._reg[key]
            on = key in visible
            mark = "[green]●[/]" if on else "[grey37]○[/]"
            ol.add_option(Option(f"{mark} {spec.label}  ({key})", id=f"col::{key}"))
        if sel is not None and sel < ol.option_count:
            ol.highlighted = sel

    def _current_col_key(self) -> Optional[str]:
        ol = self.query_one("#opt_columns", OptionList)
        if ol.highlighted is None:
            return None
        opt = ol.get_option_at_index(ol.highlighted)
        oid = opt.id or ""
        return oid.split("::", 1)[1] if oid.startswith("col::") else None

    def _toggle_col(self) -> None:
        key = self._current_col_key()
        if not key:
            return
        cols = list(self._cfg.visible_columns())
        if key in cols:
            if len(cols) > 1:               # keep at least one column
                cols.remove(key)
        else:
            cols.append(key)
        self._cfg.columns = cols
        self._rebuild_columns()
        self._apply()

    def _move_col(self, delta: int) -> None:
        key = self._current_col_key()
        cols = list(self._cfg.visible_columns())
        if not key or key not in cols:
            return
        i = cols.index(key)
        j = i + delta
        if 0 <= j < len(cols):
            cols[i], cols[j] = cols[j], cols[i]
            self._cfg.columns = cols
            self._rebuild_columns()
            self._apply()

    # ── apply + persist ───────────────────────────────────────────────────
    def _apply(self) -> None:
        """Push the working config to the app (live re-render + persist)."""
        if not self._accent_preview_dirty:
            self.app.apply_config(self._cfg)  # type: ignore[attr-defined]
            return

        preview_accent = self._cfg.theme_accent
        self._cfg.theme_accent = self._committed_accent
        self.app.apply_config(self._cfg)  # type: ignore[attr-defined]
        self._cfg.theme_accent = preview_accent
        try:
            self.app._apply_accent(preview_accent)  # type: ignore[attr-defined]
            self.app.preview_config(self._cfg)  # type: ignore[attr-defined]
        except AttributeError:
            pass

    def _preview_accent(self, accent: str) -> None:
        """Preview an accent in-place; Enter commits, ESC/close restores."""
        if accent not in _VALID_ACCENTS:
            return
        self._cfg.theme_accent = accent
        self._accent_preview_dirty = accent != self._committed_accent
        try:
            self.app._apply_accent(accent)  # type: ignore[attr-defined]
        except AttributeError:
            pass
        try:
            self.app.preview_config(self._cfg)  # type: ignore[attr-defined]
        except AttributeError:
            self.app.apply_config(self._cfg)  # type: ignore[attr-defined]
        self._set_status("theme preview: Enter to apply, Esc to restore")

    def _set_accent_select(self, accent: str) -> None:
        try:
            sel = self.query_one("#opt_accent", Select)
            sel.value = accent
        except Exception:
            pass

    def _cycle_accent(self, delta: int) -> None:
        current = self._cfg.theme_accent
        try:
            idx = _VALID_ACCENTS.index(current)
        except ValueError:
            idx = 0
        accent = _VALID_ACCENTS[(idx + delta) % len(_VALID_ACCENTS)]
        self._set_accent_select(accent)
        self._preview_accent(accent)

    def _commit_accent_preview(self) -> None:
        if not self._accent_preview_dirty:
            return
        self._committed_accent = self._cfg.theme_accent
        self._accent_preview_dirty = False
        self._apply()
        self._set_status("theme applied")

    def _restore_accent_preview(self) -> None:
        if not self._accent_preview_dirty:
            return
        self._cfg.theme_accent = self._committed_accent
        self._accent_preview_dirty = False
        self._set_accent_select(self._committed_accent)
        try:
            self.app._apply_accent(self._committed_accent)  # type: ignore[attr-defined]
        except AttributeError:
            pass
        try:
            self.app.preview_config(self._cfg)  # type: ignore[attr-defined]
        except AttributeError:
            self.app.apply_config(self._cfg)  # type: ignore[attr-defined]
        self._set_status("theme restored")

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#opt_status", Label).update(msg)
        except Exception:
            pass

    # ── events ──────────────────────────────────────────────────────────
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        mapping = {
            "opt_p_summary": "show_summary", "opt_p_trends": "show_trends",
            "opt_p_podtable": "show_podtable", "opt_p_events": "show_events",
            "opt_p_pvc": "show_pvc",
            "opt_p_alerts": "show_alerts", "opt_p_health": "show_health",
            "opt_sort_desc": "sort_desc",
            "opt_group_by_node": "group_by_node",
            "opt_hide_completed": "hide_completed",
            "opt_only_problems": "only_problems",
        }
        attr = mapping.get(cid)
        if attr:
            setattr(self._cfg, attr, event.value)
            self._apply()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        if event.select.id == "opt_sort":
            # sort_key supersedes sort_mode; keep the legacy mirror in sync.
            self._cfg.sort_key = str(event.value)
            self._cfg.sort_mode = (str(event.value)
                                   if str(event.value) in ("priority", "cpu", "mem", "name")
                                   else "priority")
        elif event.select.id == "opt_accent":
            self._preview_accent(str(event.value))
            event.stop()
            return
        elif event.select.id == "opt_summary_style":
            self._cfg.summary_style = str(event.value)
        self._apply()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._consume_input(event.input)

    def on_input_changed(self, event: Input.Changed) -> None:
        # apply numeric/text inputs on change (validated/coerced in app)
        self._consume_input(event.input)

    def _consume_input(self, inp: Input) -> None:
        iid = inp.id or ""
        val = inp.value
        try:
            if iid == "opt_interval":
                self._cfg.interval = max(1.0, float(val or "3"))
            elif iid == "opt_name_filter":
                self._cfg.name_filter = val.strip()
            elif iid == "opt_context":
                self._cfg.context = val.strip()
            elif iid == "opt_alertmanager_url":
                self._cfg.alertmanager_url = val.strip()
            # thresholds are edited via DualThresholdSlider (Thresholds tab),
            # not numeric inputs — see on_dual_threshold_slider_threshold_changed.
        except (TypeError, ValueError):
            return
        self._apply()

    def on_dual_threshold_slider_threshold_changed(
        self, event: "DualThresholdSlider.ThresholdChanged"
    ) -> None:
        """Live-apply a dragged threshold slider into the working config.

        Maps the slider's metric -> the matching ``<metric>_warn/_crit`` config
        fields, then calls ``apply_config`` so the table gauges re-color
        immediately and the change persists to the user config file.
        """
        m = event.metric
        if m in ("cpu", "mem", "pvc"):
            setattr(self._cfg, f"{m}_warn", int(event.warn))
            setattr(self._cfg, f"{m}_crit", int(event.crit))
            self._apply()
        event.stop()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        pass  # highlight only; toggle/move via buttons or keys

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        # Enter on a namespace row toggles its membership (multi-select).
        if event.option_list.id == "opt_namespaces":
            self._toggle_ns()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "opt_col_toggle":
            self._toggle_col()
        elif bid == "opt_col_up":
            self._move_col(-1)
        elif bid == "opt_col_down":
            self._move_col(1)
        elif bid == "opt_export":
            path = self.app.export_config(self._cfg)  # type: ignore[attr-defined]
            self._set_status(f"exported -> {path}" if path
                             else "export failed (PyYAML missing?)")
        elif bid in ("opt_close",):
            self.action_close()

    def on_key(self, event) -> None:
        # column reorder/toggle shortcuts when the column list has focus
        focused = self.focused
        if focused is not None and focused.id == "opt_columns":
            if event.key == "space":
                self._toggle_col()
                event.stop()
            elif event.key == "left_square_bracket":
                self._move_col(-1)
                event.stop()
            elif event.key == "right_square_bracket":
                self._move_col(1)
                event.stop()
        elif focused is not None and focused.id == "opt_namespaces":
            if event.key == "space":
                self._toggle_ns()
                event.stop()
        elif focused is not None and focused.id == "opt_accent":
            if event.key in ("up", "left"):
                self._cycle_accent(-1)
                event.stop()
            elif event.key in ("down", "right"):
                self._cycle_accent(1)
                event.stop()
            elif event.key == "enter":
                self._commit_accent_preview()
                event.stop()
            elif event.key == "escape":
                self._restore_accent_preview()
                event.stop()

    def action_close(self) -> None:
        self._restore_accent_preview()
        self.dismiss()
