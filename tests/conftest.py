from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _ensure_main_thread_event_loop() -> None:
    """Keep synchronous Textual widget construction portable on Python 3.9.

    Textual 8 still constructs a few asyncio primitives from synchronous widget
    constructors. After an ``asyncio.run(...)`` test, Python 3.9 leaves the main
    thread without a current event loop, so later sync widget tests can fail
    before the app runner starts.
    """

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
