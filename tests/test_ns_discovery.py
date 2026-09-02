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
            # a frame must exist first: before that the startup guidance rows
            # carry the failure and the toast is deliberately suppressed
            app._apply_snapshot(_good_frame(), gen=app._fetch_gen)
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
            app._apply_snapshot(_good_frame(), gen=app._fetch_gen)
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


# ── 7. the CONTEXT picker never claims a context the app is not using ────────


def test_no_context_picker_shows_unset_and_first_pick_takes_effect() -> None:
    """Launching without a kubectl current-context used to display the first
    DISCOVERED context as if it were active. The app was still querying
    kubectl's default server, and because the displayed value equalled the one
    the user would pick, selecting it posted no Changed — so that context could
    never be selected and its namespaces never loaded."""

    from textual.widgets import Select

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        app.fetcher = _FakeFetcher({"local": ["kube-system", "llm"]})  # type: ignore[assignment]
        picked: "list[str]" = []
        app.set_context = lambda name: picked.append(name)  # type: ignore[method-assign]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            # discovery found three contexts; the app itself has none selected
            sidebar.rebuild_contexts(["local", "spm-eks", "spm-eks-dev"], "")
            await pilot.pause()
            await pilot.pause()

            sel = app.query_one("#side_context", Select)
            assert sel.value == ""                     # honest: nothing selected
            labels = [label for label, _ in sel._options] if hasattr(
                sel, "_options") else []
            assert any("no context" in str(label) for label in labels)

            # picking the real context is now a genuine change
            sel.value = "local"
            for _ in range(3):
                await pilot.pause()
            assert picked == ["local"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_resolved_context_is_shown_without_an_unset_entry() -> None:
    """Once a context IS active the picker shows it, with no phantom entry."""

    from textual.widgets import Select

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="spm-eks")
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            sidebar.rebuild_contexts(["local", "spm-eks"], "spm-eks")
            await pilot.pause()
            sel = app.query_one("#side_context", Select)
            assert sel.value == "spm-eks"
            await pilot.exit(None)

    asyncio.run(drive())


def test_discovery_failure_stays_silent_before_the_first_frame() -> None:
    """The startup guidance rows already name the failure — and name it better
    ('no kube context selected'); a toast there is pure noise."""

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        notices = _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._loaded is False
            app._populate_ns_list(
                [], app._discover_gen,
                "The connection to the server localhost:8080 was refused")
            await pilot.pause()
            assert not [n for n in notices if "namespace list" in n]
            assert "unavailable" in _ns_title(app)     # the header still says it

            # after a frame has landed, the same failure IS announced
            app._apply_snapshot(_good_frame(), gen=app._fetch_gen)
            await pilot.pause()
            app._populate_ns_list([], app._discover_gen, "timed out after 6s")
            await pilot.pause()
            assert [n for n in notices if "namespace list unavailable" in n]
            await pilot.exit(None)

    asyncio.run(drive())


# ── 8. namespaces are remembered per context ─────────────────────────────────


def test_context_switch_restores_that_clusters_own_namespaces() -> None:
    """Switching clusters must not carry the previous cluster's scope over —
    namespaces that do not exist in the new cluster used to stay listed AND
    ticked (calico-* from a local cluster showing under an EKS context)."""

    async def drive() -> None:
        app = TopApp(["calico-system", "default"], discover_namespaces=False,
                     auto_refresh=False, context="local")
        app.fetcher = _FakeFetcher({}, context="local")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.cfg.namespaces_by_context["spm-eks"] = ["prod", "api"]

            app.set_context("spm-eks")
            await pilot.pause()

            # the EKS cluster's own remembered scope is adopted ...
            assert list(app.namespaces) == ["prod", "api"]
            # ... and the local cluster's scope is parked under its own key
            assert app.cfg.namespaces_by_context["local"] == [
                "calico-system", "default"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_unknown_context_starts_from_the_default_scope() -> None:
    """A cluster we have never watched starts at the built-in default, not at
    whatever the previous cluster happened to be showing."""

    async def drive() -> None:
        app = TopApp(["calico-system"], discover_namespaces=False,
                     auto_refresh=False, context="local")
        app.fetcher = _FakeFetcher({}, context="local")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.set_context("brand-new")
            await pilot.pause()
            assert list(app.namespaces) == ["default"]
            assert "calico-system" not in _ns_labels(app)
            await pilot.exit(None)

    asyncio.run(drive())


def test_ticking_namespaces_records_them_for_the_active_context() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="spm-eks")
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.set_namespaces(["prod", "api"])
            await pilot.pause()
            assert app.cfg.namespaces_by_context["spm-eks"] == ["prod", "api"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_listing_drops_namespaces_the_cluster_does_not_have() -> None:
    """The reported symptom: calico-* survived a switch to an EKS context."""

    async def drive() -> None:
        app = TopApp(["default", "calico-system", "calico-apiserver"],
                     discover_namespaces=False, auto_refresh=False,
                     context="spm-eks")
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list(["default", "prod", "api"],
                                  app._discover_gen, "")
            await pilot.pause()
            assert list(app.namespaces) == ["default"]
            labels = _ns_labels(app)
            assert "calico-system" not in labels
            assert "calico-apiserver" not in labels
            await pilot.exit(None)

    asyncio.run(drive())


def test_pruning_keeps_a_namespace_the_cluster_really_has() -> None:
    async def drive() -> None:
        app = TopApp(["prod", "gone"], discover_namespaces=False,
                     auto_refresh=False, context="spm-eks")
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list(["prod", "api"], app._discover_gen, "")
            await pilot.pause()
            assert list(app.namespaces) == ["prod"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_pruning_never_empties_the_watched_set() -> None:
    """Nothing in common: fall back to a namespace the cluster actually has."""

    async def drive() -> None:
        app = TopApp(["only-here"], discover_namespaces=False,
                     auto_refresh=False, context="spm-eks")
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._populate_ns_list(["alpha", "beta"], app._discover_gen, "")
            await pilot.pause()
            assert list(app.namespaces) == ["alpha"]   # no 'default' here
            await pilot.exit(None)

    asyncio.run(drive())


def test_failed_listing_never_prunes() -> None:
    """A failed listing lists nothing — that must not read as 'this cluster has
    no namespaces' and wipe the user's scope."""

    async def drive() -> None:
        app = TopApp(["prod", "api"], discover_namespaces=False,
                     auto_refresh=False, context="spm-eks")
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_good_frame(), gen=app._fetch_gen)
            await pilot.pause()
            app._populate_ns_list([], app._discover_gen, "timed out after 6s")
            await pilot.pause()
            assert list(app.namespaces) == ["prod", "api"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_startup_recall_restores_the_contexts_scope_once() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="spm-eks")
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.cfg.namespaces_by_context["spm-eks"] = ["prod", "api"]

            app._populate_ns_list(["default", "prod", "api"],
                                  app._discover_gen, "")
            await pilot.pause()
            assert list(app.namespaces) == ["prod", "api"]

            # a later listing must not undo a subsequent manual pick
            app.set_namespaces(["default"])
            await pilot.pause()
            app._populate_ns_list(["default", "prod", "api"],
                                  app._discover_gen, "")
            await pilot.pause()
            assert list(app.namespaces) == ["default"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_startup_recall_yields_to_namespaces_given_on_the_command_line() -> None:
    async def drive() -> None:
        app = TopApp(["typed-ns"], discover_namespaces=False, auto_refresh=False,
                     context="spm-eks",
                     reload_overrides={"base_overrides":
                                       {"cluster": {"namespaces": ["typed-ns"]}}})
        app.fetcher = _FakeFetcher({}, context="spm-eks")  # type: ignore[assignment]
        _mute(app)
        app._persist_state = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.cfg.namespaces_by_context["spm-eks"] = ["prod"]
            app._populate_ns_list(["typed-ns", "prod"], app._discover_gen, "")
            await pilot.pause()
            assert list(app.namespaces) == ["typed-ns"]
            await pilot.exit(None)

    asyncio.run(drive())
