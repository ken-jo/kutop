"""Header widgets for the kutop app (hamburger menu icon, metrics readout).

Split out of render/app.py along its class seams — pure presentation, no
fetching, no workload knowledge.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.reactive import Reactive
from textual.widget import Widget
from textual.widgets import Header, Static

from ._compat import HeaderClock, HeaderClockSpace, HeaderTitle

__all__ = ["ThemeHeaderIcon", "MetricsIndicator", "ThemeHeader"]

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
