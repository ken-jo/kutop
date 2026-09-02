"""Regression tests for the namespace-discovery lifecycle.

Reported live: launching with a slow cluster left the sidebar showing CONTEXT
``local`` next to a NAMESPACES list holding only the watched ``default`` — the
listing had failed, silently, and the only way to force a retry was to switch
contexts back and forth (which then raced: a slow listing from the previous
cluster could land as the new one's list).

The contract these tests pin down:
  * a listing carries the scope token it was started under; a result from a
    superseded scope is dropped;
  * a context change re-lists immediately and drops the old cluster's list;
  * while listing, the sidebar header says so; a failed listing says so too and
    is announced once;
  * a good refresh after a failed listing retries it, so the user never has to
    switch contexts to force one.
"""

from __future__ import annotations

import asyncio
import threading

from textual.widgets import Checkbox, Label

from kutop.model import Node, Snapshot
from kutop.render.app import TopApp
from kutop.render.sidebar import SidebarPanel


class _FakeFetcher:
    """Stands in for Fetcher: records calls, lists a scripted namespace set."""

    def __init__(self, per_context: dict, context: str = "") -> None:
        self._per_context = per_context      # {context: [ns] | Exception}
        self.context = context
        self.namespaces: "list[str]" = []
        self.total_namespaces = None
        self.calls: "list[str]" = []
        self.gate: "threading.Event | None" = None

    def current_context_name(self) -> str:
        return self.context or ""

    def list_namespaces(self) -> "list[str]":
        self.calls.append(self.context)
        if self.gate is not None:
            self.gate.wait(5)
        result = self._per_context.get(self.context, [])
        if isinstance(result, Exception):
            raise result
        return list(result)

    # the app touches these on scope changes
    def invalidate_caches(self) -> None:
        pass

    def cancel(self) -> None:
        pass


def _mute(app: TopApp) -> "list[str]":
    notices: "list[str]" = []
    app.notify = lambda msg, **kw: notices.append(str(msg))  # type: ignore[assignment]
    app.refresh_snapshot = lambda: None  # type: ignore[method-assign]
    app._request_refresh = lambda: None  # type: ignore[method-assign]
    return notices


def _ns_labels(app: TopApp) -> "list[str]":
    box = app.query_one("#side_ns_box")
    return [str(cb.label) for cb in box.query(Checkbox)]


def _ns_title(app: TopApp) -> str:
    """The NAMESPACES header as plain text (Textual 8 exposes it as .content)."""
    label = app.query_one("#side_ns_title", Label)
    content = label.content
    return content.plain if hasattr(content, "plain") else str(content)


def _good_frame() -> Snapshot:
    snap = Snapshot()
    snap.nodes = [Node(name="node-a", ready=True)]
    return snap


# ── 1. a stale listing never lands ───────────────────────────────────────────


def test_result_from_a_superseded_context_is_dropped() -> None:
    """Switching A -> B while A's listing is still running must not repopulate
    the sidebar with A's namespaces."""

    async def drive() -> None:
        # discovery off at mount so no real worker races this scenario
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="ctx-a")
        app.fetcher = _FakeFetcher(  # type: ignore[assignment]
            {"ctx-a": ["a-one", "a-two"], "ctx-b": ["b-one"]}, context="ctx-a")
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            stale_gen = app._discover_gen
            app._discover_gen += 1          # a newer scope has started meanwhile

            app._populate_ns_list(["a-one", "a-two"], stale_gen, "")
            await pilot.pause()
            assert app._discovered_ns == []
            assert "a-one" not in _ns_labels(app)

            # the current scope's answer is accepted
            app._populate_ns_list(["b-one"], app._discover_gen, "")
            await pilot.pause()
            assert "b-one" in _ns_labels(app)
            await pilot.exit(None)

    asyncio.run(drive())


def test_context_switch_relists_and_drops_the_old_clusters_namespaces() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="ctx-a")
        fake = _FakeFetcher({"ctx-a": ["a-one"], "ctx-b": ["b-one"]},
                            context="ctx-a")
        fake.gate = threading.Event()      # park the worker mid-listing
        app.fetcher = fake  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list(["a-one"], app._discover_gen, "")
            await pilot.pause()
            assert "a-one" in _ns_labels(app)

            app._discover_namespaces = True   # live path from here on
            before = app._discover_gen
            app.set_context("ctx-b")
            await pilot.pause()

            # the scope token moved and A's namespaces are gone from the picker
            assert app._discover_gen > before
            assert app._discovered_ns == []
            assert "a-one" not in _ns_labels(app)
            assert app._ns_discovering is True
            assert "loading" in _ns_title(app)
            fake.gate.set()                    # let the parked worker finish
            await pilot.exit(None)

    asyncio.run(drive())


# ── 2. the sidebar says what discovery is doing ──────────────────────────────


def test_namespaces_header_shows_loading_then_clears() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        fake = _FakeFetcher({"": ["kube-system"]})
        fake.gate = threading.Event()      # park the worker so "loading…" holds
        app.fetcher = fake  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._discover_namespaces = True
            app._start_ns_discovery()
            await pilot.pause()
            assert "loading" in _ns_title(app)

            app._populate_ns_list(["kube-system"], app._discover_gen, "")
            await pilot.pause()
            assert _ns_title(app) == "NAMESPACES"
            assert app._ns_discovering is False
            fake.gate.set()
            await pilot.exit(None)

    asyncio.run(drive())


def test_failed_listing_is_visible_and_announced_once() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=True, auto_refresh=False)
        app.fetcher = _FakeFetcher({})  # type: ignore[assignment]
        notices = _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list([], app._discover_gen, "timed out after 6s")
            await pilot.pause()

            assert "unavailable" in _ns_title(app)
            assert app._ns_discovery_failed is True
            assert [n for n in notices if "namespace list unavailable" in n]

            # the identical failure next pass stays silent
            count = len(notices)
            app._populate_ns_list([], app._discover_gen, "timed out after 6s")
            await pilot.pause()
            assert len(notices) == count
            await pilot.exit(None)

    asyncio.run(drive())


# ── 3. a good refresh retries a failed listing ───────────────────────────────


def test_good_frame_retries_a_failed_listing() -> None:
    """The cluster answering again must refill the sidebar on its own — the
    user should never have to switch contexts to force a retry."""

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=True, auto_refresh=False)
        app.fetcher = _FakeFetcher({"": ["kube-system", "llm"]})  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list([], app._discover_gen, "timed out after 6s")
            await pilot.pause()
            assert app._ns_discovery_failed is True

            before = app._discover_gen
            app._apply_snapshot(_good_frame(), gen=app._fetch_gen)
            await pilot.pause()
            assert app._discover_gen > before      # a retry was kicked
            await pilot.exit(None)

    asyncio.run(drive())


def test_good_frame_does_not_retry_a_healthy_listing() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=True, auto_refresh=False)
        app.fetcher = _FakeFetcher({"": ["kube-system"]})  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list(["kube-system"], app._discover_gen, "")
            await pilot.pause()

            before = app._discover_gen
            app._apply_snapshot(_good_frame(), gen=app._fetch_gen)
            await pilot.pause()
            assert app._discover_gen == before     # nothing to retry
            await pilot.exit(None)

    asyncio.run(drive())


# ── 4. --self-test / snapshot stay kubectl-free ──────────────────────────────


def test_discovery_disabled_never_starts_a_worker() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        fake = _FakeFetcher({"": ["kube-system"]})
        app.fetcher = fake  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app._discover_gen
            app._start_ns_discovery()
            await pilot.pause()
            assert app._discover_gen == before
            assert fake.calls == []
            await pilot.exit(None)

    asyncio.run(drive())


# ── 5. the worker carries its scope token end to end ─────────────────────────


def test_worker_passes_its_generation_through() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=True, auto_refresh=False,
                     context="ctx-a")
        app.fetcher = _FakeFetcher({"ctx-a": ["a-one"]}, context="ctx-a")  # type: ignore[assignment]
        _mute(app)
        seen: "list[tuple]" = []
        real = app._populate_ns_list
        app._populate_ns_list = (  # type: ignore[method-assign]
            lambda d, g=None, e="": (seen.append((tuple(d), g, e)), real(d, g, e))[1])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            gen = app._discover_gen + 1
            app._discover_gen = gen
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, app._discover_ns_worker, gen)
            await pilot.pause()
            assert seen and seen[-1] == (("a-one",), gen, "")
            await pilot.exit(None)

    asyncio.run(drive())


def test_worker_reports_the_listing_error() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=True, auto_refresh=False)
        app.fetcher = _FakeFetcher(  # type: ignore[assignment]
            {"": RuntimeError("timed out after 6s")})
        notices = _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, app._discover_ns_worker,
                                       app._discover_gen)
            await pilot.pause()
            assert any("timed out after 6s" in n for n in notices)
            assert "unavailable" in _ns_title(app)
            await pilot.exit(None)

    asyncio.run(drive())


# ── 6. the sidebar helper itself ─────────────────────────────────────────────


def test_sidebar_ns_status_is_plain_text() -> None:
    """A status is appended to the header, never parsed as markup."""

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            sidebar.set_ns_status("[loading]")
            await pilot.pause()
            assert "[loading]" in _ns_title(app)
            sidebar.set_ns_status("")
            await pilot.pause()
            assert _ns_title(app) == "NAMESPACES"
            await pilot.exit(None)

    asyncio.run(drive())
