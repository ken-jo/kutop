"""ResizableDataTable: the main pod table with a mouse-resizable name column.

Split out of render/app.py along its class seams.
"""

from __future__ import annotations

from typing import Optional

from textual import events
from textual.widgets import DataTable

__all__ = ["ResizableDataTable"]

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
