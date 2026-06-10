"""Presentation widgets for kutop.

These widgets hold no fetching logic and no workload knowledge. The app feeds
them already-computed data (a Snapshot, history deques, threshold-resolved
colors). Styling lives in ``theme.tcss``; only data-driven Rich markup is built
here.
"""

from __future__ import annotations

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
    Input,
    Label,
    Static,
)


from ..model import Summary

__all__ = [
    "level_color", "bar_gauge", "DualThresholdSlider", "Panel", "SummaryBar",
    "SearchBar", "TrendGraph", "ConfirmModal", "_severity_style",
    # re-exported from render/options.py for backward compatibility
    "OptionsModal", "ThemePreviewOverlay", "ThemePreviewSelect",
]

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
    def _content_event_x(self, event) -> int:
        """Mouse x in CONTENT coordinates.

        ``event.offset`` is widget-relative and includes the left border +
        padding gutter, while the track is rendered in the content area —
        mapping the raw offset made every click/drag land a gutter-width to
        the right of the cursor.
        """
        try:
            gutter_left = int(self.content_region.x - self.region.x)
        except Exception:
            gutter_left = 0
        return int(event.offset.x) - gutter_left

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.focus()
        width = self._track_width()
        value = self.value_at_x(self._content_event_x(event), width)
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
        value = self.value_at_x(self._content_event_x(event), width)
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
            if x == warn_x and x == crit_x:
                # both handles round to the same cell: show a combined marker
                # instead of hiding crit under warn
                track.append("▲", style="bold red reverse")
            elif x == warn_x:
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

    def update_summary(
        self,
        s: Summary,
        show_alerts: bool,
        cpu_thresh: "tuple[int, int]" = (75, 90),
        mem_thresh: "tuple[int, int]" = (80, 92),
    ) -> None:
        # remembered so on_resize can re-fit the tiles to the new width
        self._last_update = (s, show_alerts, cpu_thresh, mem_thresh)
        if self.style_mode == "compact":
            text = self._compact(s, show_alerts)
        else:
            text = self._tiles(s, show_alerts, cpu_thresh, mem_thresh)
        # never wrap: the widget is 2 rows high, so a wrapped line pushes the
        # value row out of view — overflow truncates (tiles are also dropped
        # whole to the available width in _tiles)
        text.no_wrap = True
        self.update(text)

    def on_resize(self, event: events.Resize) -> None:
        last = getattr(self, "_last_update", None)
        if last:
            self.update_summary(*last)

    def _avail_width(self) -> int:
        """Content width in cells, or 0 when not mounted yet (no fitting)."""
        try:
            return max(0, int(self.content_size.width))
        except Exception:
            return 0

    # ── tile layout ───────────────────────────────────────────────────────
    @staticmethod
    def _tile(label: str, value: Text, accent: str) -> Text:
        """One boxed stat tile: a top label line over a bold value line."""
        box = Text()
        box.append(f"┤ {label} ├", style=f"bold {accent}")
        box.append("\n")
        box.append(value)
        return box

    def _tiles(
        self,
        s: Summary,
        show_alerts: bool,
        cpu_thresh: "tuple[int, int]" = (75, 90),
        mem_thresh: "tuple[int, int]" = (80, 92),
    ) -> Text:
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

        def _accent(pct_: int, warn_crit: "tuple[int, int]") -> str:
            warn, crit = warn_crit
            return "red" if pct_ >= crit else "yellow" if pct_ >= warn else "green"

        cpu_accent = _accent(cpu_pct, cpu_thresh)
        mem_accent = _accent(mem_pct, mem_thresh)

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

        # Join the per-tile two-line blocks side by side with a separator,
        # dropping whole trailing tiles that don't fit the current width —
        # a wrapped tile row would push the value line out of the 2-row widget.
        return self._join_blocks(tiles, sep="  ", max_width=self._avail_width())

    @staticmethod
    def _join_blocks(blocks: "list[Text]", sep: str = "  ",
                     max_width: int = 0) -> Text:
        """Place multi-line Text blocks side by side, padding each to a tile.

        Each block is two lines (label / value); we widen each line to the
        block's max width so the columns stay aligned, then concatenate with a
        separator between tiles. With ``max_width > 0`` trailing blocks that
        would overflow it are dropped whole (at least one is always kept).
        """
        split = []
        for blk in blocks:
            lines = blk.split("\n")
            while len(lines) < 2:
                lines.append(Text(""))
            width = max(line.cell_len for line in lines) + 1
            split.append((lines, width))

        if max_width > 0:
            kept: list = []
            used = 0
            for lines, width in split:
                add = width + (len(sep) if kept else 0)
                if kept and used + add > max_width:
                    break
                kept.append((lines, width))
                used += add
            split = kept or split[:1]

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
    _HEAT_FILL = " ⣀⣤⣶⣿"  # 0..4 vertical braille fill levels (bottom-up)
    # Color ramp anchors shared by the heat strip and the gauge bar. Each entry
    # is (value, (r, g, b)); colors between anchors are linearly interpolated, so
    # a column's value maps onto a continuous gradient — the btop "heatmap" feel.
    # Both accents use the same value breakpoints (0/45/70/100); only hues differ.
    _HEAT_RAMP = {
        "cpu": [(0, (40, 200, 120)), (45, (150, 210, 60)),
                (70, (240, 200, 50)), (100, (255, 60, 60))],
        "mem": [(0, (48, 140, 255)), (45, (70, 200, 230)),
                (70, (230, 200, 60)), (100, (255, 70, 70))],
    }

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
        meter = self.query_one(".trend-meter", Static)
        # Size the meter to the Static's CONTENT width: deriving it from the
        # outer widget minus a hardcoded gutter subtracted the border/padding
        # twice (meter 4 cells short), and the old 24-cell floor made very
        # narrow panels wrap instead of truncate.
        width = int(getattr(meter.content_size, "width", 0) or 0)
        if width <= 0:
            width = self.size.width - 4
        width = max(10, min(96, width))
        meter.update(self._meter(clamped, cur, detail, width))

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
        text.append(value, style=f"bold {self._heat_color(cur)}")
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
                # gradient runs along the bar length, like btop meters
                pos = (idx + 0.5) / width * 100 if width else 0
                bar.append("━", style=self._heat_color(int(pos)))
            else:
                bar.append("─", style="grey23")
        return bar

    def _heat_color(self, value: int) -> str:
        """Map a 0..100 value onto the accent's continuous color ramp."""
        anchors = self._HEAT_RAMP.get(self._accent, self._HEAT_RAMP["cpu"])
        v = max(0, min(100, value))
        for (lo, lo_rgb), (hi, hi_rgb) in zip(anchors, anchors[1:]):
            if v <= hi:
                t = (v - lo) / (hi - lo) if hi > lo else 0.0
                r, g, b = (round(lo_rgb[i] + (hi_rgb[i] - lo_rgb[i]) * t) for i in range(3))
                return f"#{r:02x}{g:02x}{b:02x}"
        r, g, b = anchors[-1][1]
        return f"#{r:02x}{g:02x}{b:02x}"

    def _heat_rows(self, values: list[Optional[int]]) -> tuple[Text, ...]:
        rows = tuple(Text() for _ in range(self._HEAT_ROWS))
        levels = self._HEAT_ROWS * 4  # 4 vertical braille dots per cell
        for value in values:
            if value is None:
                for row in rows:
                    row.append("·", style="grey23")
                continue
            filled = round(max(0, min(100, value)) / 100 * levels)
            color = self._heat_color(value)
            for row_index, row in enumerate(rows):  # 0 = top row, last = bottom
                from_bottom = self._HEAT_ROWS - 1 - row_index
                cell = max(0, min(4, filled - from_bottom * 4))
                if cell > 0:
                    row.append(self._HEAT_FILL[cell], style=color)
                else:
                    row.append(" ")  # empty above the curve
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


# Backward-compatible re-exports: the Options/theme modals moved to
# render/options.py. Imported at the BOTTOM so options.py can import the
# primitives defined above without a circular-import failure.
from .options import (  # noqa: E402
    OptionsModal,
    ThemePreviewOverlay,
    ThemePreviewSelect,
)
