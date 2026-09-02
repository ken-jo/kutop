"""Single home for kutop's imports of Textual PRIVATE modules.

The exact dependency pins (``textual==8.2.8``, ``rich==15.0.0`` in
pyproject.toml) exist because these internals carry no stability guarantee.
Keeping every private import behind this one module means a Textual bump that
moves them breaks ONE file with one clear error — not scattered call sites.
The latest-deps canary job in CI exercises exactly this surface.
"""

from __future__ import annotations

from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderTitle
from textual.widgets._select import SelectCurrent, SelectOverlay

__all__ = [
    "HeaderClock",
    "HeaderClockSpace",
    "HeaderTitle",
    "SelectCurrent",
    "SelectOverlay",
]
