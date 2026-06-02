"""Presentation widgets for kutop.

These widgets hold no fetching logic and no workload knowledge. The app feeds
them already-computed data (a Snapshot, history deques, threshold-resolved
colors). Styling lives in ``theme.tcss``; only data-driven Rich markup is built
here.
"""

from __future__ import annotations

import asyncio
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
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets._select import SelectCurrent, SelectOverlay
from textual.widgets.option_list import Option

from ..config import (
    Config,
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

    Plain text panels use the same Textual border-title chrome as the
    table-backed panels. The background-fill option is scoped to panel bodies
    and stable table/search chrome in CSS so titles stay aligned on the border
    line instead of becoming content rows.

    Pure presentation: it holds no fetching logic and no workload knowledge.
    """

    DEFAULT_CLASSES = "kpanel"

    def __init__(self, title: str = "", **kwargs) -> None:
        classes = str(kwargs.pop("classes", "") or "")
        merged_classes = " ".join(
            dict.fromkeys([*self.DEFAULT_CLASSES.split(), *classes.split()])
        )
        super().__init__(classes=merged_classes, **kwargs)
        self._title = title
        self._body: "Optional[Static]" = None
        #: last renderable handed to :meth:`set_body` (kept so callers/tests can
        #: introspect the rendered content without reaching into Static internals).
        self._last_renderable: RenderableType = ""

    def compose(self) -> ComposeResult:
        self._body = Static(id=None, classes="kpanel-body")
        yield self._body

    def on_mount(self) -> None:
        if self._title:
            self.border_title = self._title

    def set_title(self, title: str) -> None:
        self._title = title
        self.border_title = title

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


# ── trend graph (thin history meter + live label) ─────────────────────────────


class TrendGraph(Vertical):
    """A compact btop-style live meter for CPU or MEM overall trend.

    Textual's multi-line ``Sparkline`` reads like a large rectangular fill in a
    short panel. This widget instead renders a one-line historical spark strip
    plus one current-value gauge, both on a fixed 0..100 scale so CPU and MEM
    remain visually comparable.
    """

    _SCALE_MIN = 0
    _SCALE_MAX = 100
    _HEAT_ROWS = 3
    _HEAT_CHARS = "⠂⠆⡆⣆⣧⣷⣿"

    def __init__(self, title: str, accent: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._accent = accent

    def compose(self) -> ComposeResult:
        yield Static("--", classes=f"trend-meter {self._accent}")

    def on_mount(self) -> None:
        self.border_title = self._title

    def update_trend(self, history: list[int], detail: str) -> None:
        clamped = [max(self._SCALE_MIN, min(self._SCALE_MAX, int(v))) for v in history]
        cur = clamped[-1] if clamped else 0
        width = max(24, min(96, self.size.width - 4))
        self.query_one(".trend-meter", Static).update(self._meter(clamped, cur, detail, width))

    def _meter(self, history: list[int], cur: int, detail: str, width: int) -> Text:
        spark_width = max(10, width - 5)
        spark_values = self._fit_history(history, spark_width)
        text = Text()
        self._append_now_line(text, cur, detail, width)
        text.append("\n")
        text.append("heat ", style="dim")
        for idx, row in enumerate(self._heat_rows(spark_values)):
            if idx:
                text.append("\n")
                text.append("     ", style="dim")
            text.append(row)
        return text

    def _append_now_line(self, text: Text, cur: int, detail: str, width: int) -> None:
        value = f"{cur:>3}%"
        left_len = len("now ") + len(value) + 1
        min_bar_width = 10
        detail_width = max(0, width - left_len - min_bar_width - 1)
        right = detail[:detail_width]
        bar_width = max(min_bar_width, width - left_len - len(right) - 1)
        text.append("now ", style="dim")
        text.append(value, style=self._style_for(cur))
        text.append(" ")
        text.append(self._inline_bar(cur, bar_width))
        if right:
            text.append(" ")
            text.append(right, style="bold")

    def _inline_bar(self, cur: int, width: int) -> Text:
        fill = max(0, min(width, int(round(cur * width / 100))))
        bar = Text()
        for idx in range(width):
            if idx < fill:
                bar.append("━", style=self._style_for(cur))
            else:
                bar.append("─", style="grey23")
        return bar

    def _heat_rows(self, values: list[Optional[int]]) -> tuple[Text, ...]:
        rows = tuple(Text() for _ in range(self._HEAT_ROWS))
        for value in values:
            if value is None:
                for row in rows:
                    row.append("·", style="grey23")
                continue
            idx = max(0, min(len(self._HEAT_CHARS) - 1, int(value * len(self._HEAT_CHARS) / 101)))
            char = self._HEAT_CHARS[idx]
            style = self._style_for(value)
            active_rows = max(1, min(self._HEAT_ROWS, (value + 32) // 33))
            for row_index, row in enumerate(rows):
                if row_index >= self._HEAT_ROWS - active_rows:
                    row.append(char, style=style)
                else:
                    row.append("·", style="grey37")
        return rows

    @staticmethod
    def _fit_history(history: list[int], width: int) -> list[Optional[int]]:
        if width <= 0:
            return []
        if not history:
            return [None] * width
        tail = history[-width:]
        pad_value = tail[0]
        return [pad_value] * (width - len(tail)) + tail

    def _style_for(self, value: int) -> str:
        if value >= 85:
            return "bold red"
        if value >= 70:
            return "bold yellow"
        return "bold cyan" if self._accent == "mem" else "bold green"


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


class ThemeMenuModal(ModalScreen):
    """Hamburger menu with native actions and Options entry."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "commit", "Apply"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="theme_menu"):
            yield Label("MENU", id="theme_menu_title")
            yield Label("Keys · Screenshot · Quit · Options", id="theme_menu_hint")
            yield OptionList(id="theme_menu_list")

    def on_mount(self) -> None:
        ol = self.query_one("#theme_menu_list", OptionList)
        ol.add_option(Option("Keys", id="action::keys"))
        ol.add_option(Option("Screenshot", id="action::screenshot"))
        ol.add_option(Option("Quit", id="action::quit"))
        ol.add_option(Option("Options / Settings", id="action::options"))
        ol.highlighted = 0

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        oid = event.option.id or ""
        if oid.startswith("action::"):
            self._run_action(oid)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        menu = self.query_one("#theme_menu")
        if menu.region.contains_point((event.screen_x, event.screen_y)):
            return
        event.stop()
        event.prevent_default()
        self.dismiss(None)

    def action_commit(self) -> None:
        ol = self.query_one("#theme_menu_list", OptionList)
        if ol.highlighted is not None:
            opt = ol.get_option_at_index(ol.highlighted)
            oid = opt.id or ""
            if oid.startswith("action::"):
                self._run_action(oid)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _run_action(self, oid: str) -> None:
        action = oid.split("::", 1)[1]
        self.dismiss(action)
        if action == "keys":
            asyncio.create_task(self.app.run_action("app.show_help_panel"))
        elif action == "screenshot":
            asyncio.create_task(self.app.run_action("app.screenshot"))
        elif action == "quit":
            asyncio.create_task(self.app.run_action("app.quit"))
        elif action == "options":
            self.app.action_open_options()  # type: ignore[attr-defined]


class ThemePreviewOverlay(SelectOverlay):
    """Select overlay that exposes highlight/escape as theme preview events."""

    class Preview(Message):
        def __init__(self, overlay: "ThemePreviewOverlay", option_index: int) -> None:
            super().__init__()
            self.overlay = overlay
            self.option_index = option_index

    class Restore(Message):
        pass

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        self.post_message(self.Preview(self, event.option_index))

    def watch__mouse_hovering_over(self, option_index: Optional[int]) -> None:
        if option_index is None:
            return
        if 0 <= option_index < len(self.options):
            option = self.options[option_index]
            if not option.disabled:
                self.post_message(self.Preview(self, option_index))

    def action_dismiss(self) -> None:
        self.post_message(self.Restore())
        super().action_dismiss()


class ThemePreviewSelect(Select):
    """A normal Select whose open menu previews highlighted theme rows."""

    def compose(self) -> ComposeResult:
        yield SelectCurrent(self.prompt)
        yield ThemePreviewOverlay(type_to_search=self._type_to_search).data_bind(
            compact=Select.compact
        )


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

    def __init__(
        self,
        cfg: Config,
        discovered_ns: "Optional[list[str]]" = None,
        context_names: "Optional[list[str]]" = None,
        themes: "Optional[list[str]]" = None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._reg = build_column_registry()
        self._themes = list(themes or [cfg.theme])
        if cfg.theme not in self._themes:
            self._themes.insert(0, cfg.theme)
        self._committed_theme = cfg.theme
        self._preview_theme_name = cfg.theme
        self._theme_preview_dirty = False
        self._ready_for_input = False
        # All namespaces we know about for the multi-select: the live cluster
        # discovery (if any), unioned with whatever is currently selected so a
        # config-only namespace is never dropped from the list.
        ns_all = list(discovered_ns or [])
        for ns in cfg.namespaces:
            if ns not in ns_all:
                ns_all.append(ns)
        self._ns_all = ns_all
        contexts = list(context_names or [])
        if cfg.context and cfg.context not in contexts:
            contexts.insert(0, cfg.context)
        self._context_options = [("", "current context"), *[(ctx, ctx) for ctx in contexts]]

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
                        with Vertical(classes="opt_field"):
                            yield Label("interval", classes="opt_label")
                            yield Input(value=str(c.interval), placeholder="seconds",
                                        id="opt_interval", classes="opt_input",
                                        compact=True)
                            yield Label("refresh seconds, min 1.0", classes="opt_hint")
                        with Vertical(classes="opt_field"):
                            yield Label("sort_key", classes="opt_label")
                            yield Select(
                                [(m, m) for m in SORTABLE_KEYS],
                                value=c.sort_key, id="opt_sort", allow_blank=False,
                                compact=True,
                            )
                            yield Label("sort by any column; 's' cycles in-app",
                                        classes="opt_hint")
                        yield Checkbox("Sort descending (▼)", value=c.sort_desc,
                                       id="opt_sort_desc", classes="opt_check",
                                       compact=True)
                        with Vertical(classes="opt_field"):
                            yield Label("theme", classes="opt_label")
                            yield ThemePreviewSelect(
                                [(name, name) for name in self._themes],
                                value=c.theme, id="opt_theme", allow_blank=False,
                                compact=True,
                            )
                            yield Label("Up/Down preview, Enter apply, Esc restore",
                                        classes="opt_hint")
                        yield Checkbox("Fill panel backgrounds",
                                       value=c.panel_backgrounds,
                                       id="opt_panel_backgrounds",
                                       classes="opt_check", compact=True)
                        with Vertical(classes="opt_field"):
                            yield Label("summary_style", classes="opt_label")
                            yield Select(
                                [(s, s) for s in _VALID_SUMMARY_STYLES],
                                value=c.summary_style, id="opt_summary_style",
                                allow_blank=False, compact=True,
                            )
                            yield Label("top header layout", classes="opt_hint")
                        yield Checkbox("Group pods by node (topology)",
                                       value=c.group_by_node, id="opt_group_by_node",
                                       classes="opt_check", compact=True)

                        # Filters live alongside View (which pods the table shows).
                        # Name search is intentionally runtime-only via '/' and
                        # is not exposed here so it cannot look like saved config.
                        yield Label("FILTERS", classes="opt_section")
                        yield Checkbox("Hide completed (Succeeded/Completed) pods",
                                       value=c.hide_completed, id="opt_hide_completed",
                                       classes="opt_check", compact=True)
                        yield Checkbox("Only problems (non-Running / restarts>0 / oom)",
                                       value=c.only_problems, id="opt_only_problems",
                                       classes="opt_check", compact=True)

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
                                       id="opt_p_summary", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Trend graphs", value=c.show_trends,
                                       id="opt_p_trends", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Pod table", value=c.show_podtable,
                                       id="opt_p_podtable", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Warning events", value=c.show_events,
                                       id="opt_p_events", classes="opt_check",
                                       compact=True)
                        yield Checkbox("PVC storage", value=c.show_pvc, id="opt_p_pvc",
                                       classes="opt_check", compact=True)
                        yield Checkbox("Alerts (AlertManager)", value=c.show_alerts,
                                       id="opt_p_alerts", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Health row (workload probes)",
                                       value=c.show_health, id="opt_p_health",
                                       classes="opt_check", compact=True)

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
                        with Vertical(classes="opt_field"):
                            yield Label("context", classes="opt_label")
                            yield Select(
                                [(label, value) for value, label in self._context_options],
                                value=c.context, id="opt_context", allow_blank=False,
                                compact=True,
                            )
                            yield Label("kubeconfig context; current = default",
                                        classes="opt_hint")

                # Profile identity + cluster-linked probe config ─────────────
                with TabPane("Profile", id="opt_tab_profile"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label(f"  active profile: {c.profile_name}  (name only)",
                                    classes="opt_hint", id="opt_profile_lbl")
                        yield Label("PROBES  (cluster-linked; opt-in)",
                                    classes="opt_section")
                        with Vertical(classes="opt_field"):
                            yield Label("alertmanager_url", classes="opt_label")
                            yield Input(value=c.alertmanager_url, id="opt_alertmanager_url",
                                        classes="opt_input",
                                        placeholder="http://…/api/v2/alerts",
                                        compact=True)
                            yield Label("blank = alerts panel hidden", classes="opt_hint")
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
        self.call_after_refresh(self._enable_input)

    def _enable_input(self) -> None:
        self._ready_for_input = True

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
        if self._theme_preview_dirty:
            preview_theme = self._cfg.theme
            self._cfg.theme = self._committed_theme
            self.app.apply_config(self._cfg)  # type: ignore[attr-defined]
            self._cfg.theme = preview_theme
            self.app.preview_theme(preview_theme)  # type: ignore[attr-defined]
            return
        self.app.apply_config(self._cfg)  # type: ignore[attr-defined]

    def _set_theme_select(self, theme: str) -> None:
        try:
            self.query_one("#opt_theme", Select).value = theme
        except Exception:
            pass

    def _preview_theme(self, theme: str) -> None:
        if theme not in self._themes:
            return
        self._preview_theme_name = theme
        self._theme_preview_dirty = theme != self._committed_theme
        self.app.preview_theme(theme)  # type: ignore[attr-defined]
        if self._theme_preview_dirty:
            self._set_status("theme preview: Enter to apply, Esc to restore")
        else:
            self._set_status("")

    def _commit_theme_preview(self) -> None:
        if not self._theme_preview_dirty:
            return
        self._cfg.theme = self._preview_theme_name
        self.app.commit_theme(self._cfg.theme)  # type: ignore[attr-defined]
        self._committed_theme = self._cfg.theme
        self._theme_preview_dirty = False
        self._set_status("theme applied")

    def _restore_theme_preview(self) -> None:
        if not self._theme_preview_dirty:
            return
        self._preview_theme_name = self._committed_theme
        self._theme_preview_dirty = False
        self._set_theme_select(self._committed_theme)
        self.app.preview_theme(self._committed_theme)  # type: ignore[attr-defined]
        self._set_status("theme restored")

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#opt_status", Label).update(msg)
        except Exception:
            pass

    # ── events ──────────────────────────────────────────────────────────
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if not self._ready_for_input:
            return
        cid = event.checkbox.id or ""
        mapping = {
            "opt_p_summary": "show_summary", "opt_p_trends": "show_trends",
            "opt_p_podtable": "show_podtable", "opt_p_events": "show_events",
            "opt_p_pvc": "show_pvc",
            "opt_p_alerts": "show_alerts", "opt_p_health": "show_health",
            "opt_sort_desc": "sort_desc",
            "opt_panel_backgrounds": "panel_backgrounds",
            "opt_group_by_node": "group_by_node",
            "opt_hide_completed": "hide_completed",
            "opt_only_problems": "only_problems",
        }
        attr = mapping.get(cid)
        if attr:
            setattr(self._cfg, attr, event.value)
            self._apply()

    def on_select_changed(self, event: Select.Changed) -> None:
        if not self._ready_for_input:
            return
        if event.value is Select.BLANK:
            return
        if event.select.id == "opt_sort":
            # sort_key supersedes sort_mode; keep the legacy mirror in sync.
            self._cfg.sort_key = str(event.value)
            self._cfg.sort_mode = (str(event.value)
                                   if str(event.value) in ("priority", "cpu", "mem", "name")
                                   else "priority")
        elif event.select.id == "opt_theme":
            self._preview_theme_name = str(event.value)
            self._commit_theme_preview()
            event.stop()
            return
        elif event.select.id == "opt_context":
            self._cfg.context = str(event.value)
        elif event.select.id == "opt_summary_style":
            self._cfg.summary_style = str(event.value)
        self._apply()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._ready_for_input:
            return
        self._consume_input(event.input)

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._ready_for_input:
            return
        # apply numeric/text inputs on change (validated/coerced in app)
        self._consume_input(event.input)

    def _consume_input(self, inp: Input) -> None:
        iid = inp.id or ""
        val = inp.value
        try:
            if iid == "opt_interval":
                self._cfg.interval = max(1.0, float(val or "3"))
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
        if not self._ready_for_input:
            return
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

    def on_theme_preview_overlay_preview(
        self, event: ThemePreviewOverlay.Preview
    ) -> None:
        select = event.overlay.parent
        if select is None or select.id != "opt_theme":
            return
        try:
            theme = str(select._options[event.option_index][1])  # type: ignore[attr-defined]
        except Exception:
            return
        self._preview_theme(theme)

    def on_theme_preview_overlay_restore(
        self, event: ThemePreviewOverlay.Restore
    ) -> None:
        if self._theme_preview_dirty:
            self._restore_theme_preview()
            event.stop()

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

    def action_close(self) -> None:
        self._restore_theme_preview()
        self.dismiss()
