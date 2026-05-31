"""Compatibility namespace for the old kubetop import name.

New code should import :mod:`kutop`. This module keeps legacy imports such as
``import kubetop.config`` working by exposing kutop's package path.
"""

from __future__ import annotations

import kutop as _kutop
from kutop import __version__

__path__ = _kutop.__path__

__all__ = ["__version__"]
