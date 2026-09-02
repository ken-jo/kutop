"""Renderer fixes from the 2026-09 review pass (kutop/render/app.py).

One section per fix:
  * hot-reload (R) re-runs the CLI's own layering instead of dropping flags;
  * the pod-name filter delegates to the shared kutop.regexsafe screening;
  * event timestamps parse via model.parse_timestamp (nanosecond precision);
  * a pods-only fetch failure keeps the previous pod list on screen;
  * a full refresh failure labels the panel 'PODS · stale Ns';
  * set_profile only overwrites what the profile actually supplies, and does
    not double-fire the refresh _adopt_config already kicked;
  * a probe-only config change forces a heavy cycle;
  * _force_heavy is read-and-cleared atomically under a lock;
  * mount does not shell out to kubectl on the UI thread;
  * event rows carry content-derived keys, resolved against what was rendered.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import tempfile
import threading
import time
from datetime import timezone

import pytest
from textual.widgets import DataTable

from kutop.config import Config, Profile
from kutop.model import Event, Node, Pod, Snapshot
from kutop.render.app import TopApp, _fmt_event_ts


# ── helpers ───────────────────────────────────────────────────────────────────

_TMP_FILES: "list[str]" = []


def _tmp_config(body: str) -> str:
    """A throwaway user config file, cleaned up at the end of the module."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    fh.write(body)
    fh.close()
    _TMP_FILES.append(fh.name)
    return fh.name


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tmp_configs():
    yield
    for path in _TMP_FILES:
        try:
            os.unlink(path)
        except OSError:
            pass


def _pod_snapshot(*names: str) -> Snapshot:
    snap = Snapshot()
    snap.nodes = [Node(name="node-a", ready=True)]
    snap.pods = [Pod(name=n, namespace="default", node="node-a",
                     phase="Running", ready="1/1") for n in names]
    return snap


def _row_keys(table: DataTable) -> "list[str]":
    return [str(key.value) for key in table.rows]


def _mute(app: TopApp) -> None:
    """No toasts, and never let a test kick a real kubectl fetch."""
    app.notify = lambda msg, **kw: None  # type: ignore[assignment]
    app.refresh_snapshot = lambda: None  # type: ignore[method-assign]


# ── 1. hot-reload keeps the CLI layering ──────────────────────────────────────


def test_reload_config_keeps_cli_overrides() -> None:
    """R must not silently drop the --context the session was launched with."""
    path = _tmp_config("cluster:\n  context: prod\n  namespaces: [default]\n")

    async def drive() -> None:
        app = TopApp(
            ["default"], context="staging", discover_namespaces=False,
            auto_refresh=False, config_path=path,
            reload_overrides={"cli_overrides": {"cluster": {"context": "staging"}}},
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            assert app.context == "staging"
            app.action_reload_config()
            await pilot.pause()
            # the file says 'prod'; the flag the user typed still wins
            assert app.cfg.context == "staging"
            assert app.context == "staging"
            await pilot.exit(None)

    asyncio.run(drive())


def test_reload_config_without_overrides_is_unchanged() -> None:
    """Default reload_overrides=None keeps the historical behaviour."""
    path = _tmp_config("cluster:\n  context: prod\n  namespaces: [default]\n")

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     config_path=path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            app.action_reload_config()
            await pilot.pause()
            assert app.cfg.context == "prod"
            await pilot.exit(None)

    asyncio.run(drive())


# ── 2. the filter delegates to kutop.regexsafe ────────────────────────────────


def test_filter_wrappers_delegate_to_regexsafe() -> None:
    from kutop import regexsafe

    # the class constants ARE the module's, not private copies that can drift
    assert TopApp._REGEX_META is regexsafe.REGEX_META
    assert TopApp._REGEX_MAX_LEN == regexsafe.REGEX_MAX_LEN
    # behaviour preserved through the wrappers
    assert TopApp._has_nested_quantifier("(a+)+b") is True
    assert TopApp._has_nested_quantifier("(a+)b") is False
    assert TopApp._safe_regex("web") is None             # plain -> substring
    assert TopApp._safe_regex("web-[0-9]+") is not None   # real regex
    assert TopApp._safe_regex("(a+)+b") is None          # catastrophic
    assert TopApp._safe_regex("web-[") is None           # invalid
    assert TopApp._safe_regex("a." + "x" * 400) is None  # over-long
    assert TopApp._safe_regex("") is None
    assert TopApp._term_is_regex("web-[0-9]+") is True
    assert TopApp._term_is_regex("web") is False
    # still case-insensitive
    assert TopApp._safe_regex("WEB-[0-9]+").flags & re.IGNORECASE


def test_filter_matcher_still_matches() -> None:
    app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
    rx = app._compile_filter("web-[0-9]+")
    assert rx("WEB-12") and not rx("api-12")
    plain = app._compile_filter("(a+)+b")          # unsafe -> substring
    assert plain("x(a+)+by") and not plain("aaab")


# ── 3. event timestamps ───────────────────────────────────────────────────────


def test_fmt_event_ts_handles_nanosecond_precision() -> None:
    assert _fmt_event_ts("2024-01-01T00:00:00.123456789Z", timezone.utc) == "00:00:00"
    # the shapes already supported keep working
    assert _fmt_event_ts("2024-01-01T12:34:56Z", timezone.utc) == "12:34:56"
    assert _fmt_event_ts("2024-01-01T12:34:56.12Z", timezone.utc) == "12:34:56"
    assert _fmt_event_ts("2024-01-01T12:34:56", timezone.utc) == "12:34:56"
    assert _fmt_event_ts("", timezone.utc) == "-"
    assert _fmt_event_ts("not-a-timestamp", timezone.utc) == "not-a-timestamp"


# ── 4. a pods-only failure keeps the previous pods ────────────────────────────


def test_pods_failure_keeps_previous_pod_list() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices: "list[str]" = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append(str(msg)))

            app._apply_snapshot(_pod_snapshot("web-0", "web-1"))
            await pilot.pause()
            assert [p.name for p in app.snapshot.pods] == ["web-0", "web-1"]

            # cycle 2: the nodes answered, the pod list did not
            degraded = Snapshot()
            degraded.nodes = [Node(name="node-a", ready=True)]
            degraded.error = "get pods -A: timed out after 6s"
            degraded.errors = ["get pods -A: timed out after 6s"]
            app._apply_snapshot(degraded)
            await pilot.pause()

            assert [p.name for p in app.snapshot.pods] == ["web-0", "web-1"]
            mt = app.query_one("#main_table", DataTable)
            keys = _row_keys(mt)
            assert "pod:default/web-0" in keys and "pod:default/web-1" in keys
            assert any("pods list failed — showing previous pods" in n
                       for n in notices)
            # the carried frame is NOT a good refresh: the summary bar recounts
            # the carried pods and the stale marker stays on
            assert app.snapshot.summary.pods_running == 2
            assert str(mt.border_title).startswith("PODS · stale")

            # a sustained outage stays visible even though the toast is deduped
            carried_toasts = len(notices)
            for _ in range(3):
                again = Snapshot()
                again.nodes = [Node(name="node-a", ready=True)]
                again.error = "get pods -A: timed out after 6s"
                again.errors = ["get pods -A: timed out after 6s"]
                app._apply_snapshot(again)
                await pilot.pause()
                assert str(mt.border_title).startswith("PODS · stale")
                assert [p.name for p in app.snapshot.pods] == ["web-0", "web-1"]
            assert len(notices) == carried_toasts   # deduped, title carries it

            # recovery clears the marker
            app._apply_snapshot(_pod_snapshot("web-0", "web-1"))
            await pilot.pause()
            assert str(mt.border_title) == "PODS"
            await pilot.exit(None)

    asyncio.run(drive())


def test_empty_scope_without_pods_failure_still_empties() -> None:
    """A genuinely empty namespace must NOT keep showing the old pods."""

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            app._apply_snapshot(_pod_snapshot("web-0"))
            await pilot.pause()

            empty = Snapshot()
            empty.nodes = [Node(name="node-a", ready=True)]
            empty.error = "top pods: metrics unavailable"   # not a pods source
            empty.errors = ["top pods: metrics unavailable"]
            app._apply_snapshot(empty)
            await pilot.pause()
            assert app.snapshot.pods == []
            await pilot.exit(None)

    asyncio.run(drive())


def test_pods_failure_on_first_load_keeps_guidance_rows() -> None:
    """Nothing to carry before the first snapshot: guidance rows still win."""

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            snap = Snapshot()
            snap.error = "pods: cluster unreachable"
            snap.errors = ["pods: cluster unreachable"]
            app._apply_snapshot(snap)
            await pilot.pause()
            assert app.snapshot.pods == []
            assert "startup_error" in _row_keys(
                app.query_one("#main_table", DataTable))
            await pilot.exit(None)

    asyncio.run(drive())


# ── 5. stale indicator ────────────────────────────────────────────────────────


def test_full_failure_marks_pods_panel_stale() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            mt = app.query_one("#main_table", DataTable)

            app._apply_snapshot(_pod_snapshot("web-0"))
            await pilot.pause()
            assert mt.border_title == "PODS"

            failed = Snapshot()
            failed.error = "nodes: cluster down"
            failed.errors = ["nodes: cluster down"]
            app._apply_snapshot(failed)
            await pilot.pause()
            assert str(mt.border_title).startswith("PODS · stale")

            # the age keeps ticking on every failed retry
            app._last_good_refresh -= 12
            app._apply_snapshot(failed)
            await pilot.pause()
            assert str(mt.border_title).startswith("PODS · stale 1")

            # ...and the next good frame restores the plain title
            app._apply_snapshot(_pod_snapshot("web-0"))
            await pilot.pause()
            assert mt.border_title == "PODS"
            await pilot.exit(None)

    asyncio.run(drive())


# ── 6. set_profile honours what the profile supplies ──────────────────────────


def _switch_profile(app: TopApp, profile: Profile, monkeypatch) -> None:
    monkeypatch.setattr("kutop.render.app.load_profile", lambda name: profile)
    app.set_profile(profile.name)


def test_set_profile_keeps_user_values_the_profile_omits(monkeypatch) -> None:
    path = _tmp_config("view:\n  timezone: Asia/Seoul\n"
                       "probes:\n  alertmanager_url: http://am.example/\n")

    async def drive() -> None:
        cfg = Config(timezone="Asia/Seoul", namespaces=["default"],
                     alertmanager_url="http://am.example/")
        app = TopApp(["default"], config=cfg, discover_namespaces=False,
                     auto_refresh=False, config_path=path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            bare = Profile(name="bare", namespaces=["team-a"])
            assert not bare.timezone and not bare.alertmanager_url
            _switch_profile(app, bare, monkeypatch)
            await pilot.pause()
            # the profile supplies neither: the USER's values survive
            assert app.cfg.timezone == "Asia/Seoul"
            assert app.cfg.alertmanager_url == "http://am.example/"
            assert app.cfg.health_probes == []
            # what the profile DOES supply still wins
            assert app.cfg.namespaces == ["team-a"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_set_profile_applies_supplied_values(monkeypatch) -> None:
    path = _tmp_config("view:\n  timezone: Asia/Seoul\n")

    async def drive() -> None:
        cfg = Config(timezone="Asia/Seoul", namespaces=["default"])
        app = TopApp(["default"], config=cfg, discover_namespaces=False,
                     auto_refresh=False, config_path=path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            rich = Profile(name="rich", namespaces=["team-a"], timezone="UTC",
                           alertmanager_url="http://prof.example/")
            _switch_profile(app, rich, monkeypatch)
            await pilot.pause()
            assert app.cfg.timezone == "UTC"
            assert app.cfg.alertmanager_url == "http://prof.example/"
            await pilot.exit(None)

    asyncio.run(drive())


def test_set_profile_does_not_double_refresh(monkeypatch) -> None:
    """_adopt_config already refetches on a scope change: don't do it twice."""
    path = _tmp_config("cluster:\n  namespaces: [default]\n")

    async def drive() -> None:
        cfg = Config(namespaces=["default"])
        app = TopApp(["default"], config=cfg, discover_namespaces=False,
                     auto_refresh=False, config_path=path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            calls: "list[int]" = []
            monkeypatch.setattr(TopApp, "_request_refresh",
                                lambda self: calls.append(1))
            _switch_profile(app, Profile(name="scoped", namespaces=["team-a"]),
                            monkeypatch)
            await pilot.pause()
            assert len(calls) == 1
            await pilot.exit(None)

    asyncio.run(drive())


# ── 7. a probe-only change forces a heavy cycle ───────────────────────────────


def test_probe_only_change_requests_refresh(monkeypatch) -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            calls: "list[int]" = []
            monkeypatch.setattr(TopApp, "_request_refresh",
                                lambda self: calls.append(1))

            cfg = copy.deepcopy(app.cfg)
            cfg.alertmanager_url = "http://am.example/"   # probes only
            app._adopt_config(cfg, persist=False)
            await pilot.pause()
            assert len(calls) == 1

            # a no-op adoption must not refetch
            app._adopt_config(copy.deepcopy(app.cfg), persist=False)
            await pilot.pause()
            assert len(calls) == 1
            await pilot.exit(None)

    asyncio.run(drive())


# ── 8. _force_heavy read/clear is atomic ──────────────────────────────────────


def test_force_heavy_swap_happens_under_the_lock() -> None:
    """A flag raised while the swap's lock is held must not be lost."""
    app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
    app._loaded = True
    app._fetch_gen = 3
    app._fetching = True
    app._force_heavy = False
    started = threading.Event()
    seen: "list[bool]" = []

    class FetcherStub:
        def fetch_core(self) -> Snapshot:
            started.set()
            return Snapshot(pods=[Pod(name="p", namespace="default")])

        def enrich_snapshot(self, snap: Snapshot, *, heavy: bool) -> Snapshot:
            seen.append(heavy)
            return snap

    app.fetcher = FetcherStub()  # type: ignore[assignment]
    app.refresh_snapshot = lambda: None  # type: ignore[method-assign]
    app.call_from_thread = lambda cb, *a: None  # type: ignore[method-assign]

    worker = threading.Thread(target=lambda: app._fetch_worker(3))
    with app._cadence_lock:
        worker.start()
        time.sleep(0.05)
        # the worker cannot have read the flag yet: the swap is inside the lock
        assert not started.is_set()
        app._force_heavy = True     # UI thread raises it in the critical window
    worker.join(5)
    assert not worker.is_alive()

    assert seen == [True]             # the flag was honoured...
    assert app._force_heavy is False  # ...and consumed exactly once


# ── 9. mount does not shell out to kubectl ────────────────────────────────────


class _ContextFetcher:
    """Records which thread asked kubectl for the current context name."""

    def __init__(self, namespaces: "list[str]" = None) -> None:
        self.threads: "list[threading.Thread]" = []
        self._namespaces = list(namespaces or [])
        self.total_namespaces = None

    def current_context_name(self) -> str:
        self.threads.append(threading.current_thread())
        return "ctx-x"

    def list_namespaces(self) -> "list[str]":
        return list(self._namespaces)

    def cancel(self) -> None:
        pass


def test_mount_resolves_context_off_the_ui_thread() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=True)
        fake = _ContextFetcher()
        app.fetcher = fake  # type: ignore[assignment]
        app.refresh_snapshot = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # mount itself never shells out
            assert fake.threads == []
            assert app._resolved_context == ""

            # the discovery worker does, on its own thread, and pushes back
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, app._discover_ns_worker)
            await pilot.pause()

            assert len(fake.threads) == 1
            assert fake.threads[0] is not threading.main_thread()
            assert app._resolved_context == "ctx-x"
            await pilot.exit(None)

    asyncio.run(drive())


def test_discovery_reports_cluster_size_to_the_fetcher() -> None:
    """The `-A` consolidation needs to know how big the cluster is."""

    async def drive() -> None:
        app = TopApp(["team-a"], discover_namespaces=False, auto_refresh=False)
        fake = _ContextFetcher(["team-a", "team-b", "kube-system"])
        app.fetcher = fake  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert fake.total_namespaces is None

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, app._discover_ns_worker)
            await pilot.pause()
            assert fake.total_namespaces == 3
            await pilot.exit(None)

    asyncio.run(drive())


def test_discovery_failure_leaves_cluster_size_unknown() -> None:
    """An empty/failed listing must not claim the cluster has 0 namespaces."""

    async def drive() -> None:
        app = TopApp(["team-a"], discover_namespaces=False, auto_refresh=False)
        fake = _ContextFetcher([])
        app.fetcher = fake  # type: ignore[assignment]
        _mute(app)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, app._discover_ns_worker)
            await pilot.pause()
            assert fake.total_namespaces is None
            await pilot.exit(None)

    asyncio.run(drive())


# ── 10. stable event row keys ─────────────────────────────────────────────────


def test_event_row_selection_uses_rendered_rows() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _mute(app)
            snap = _pod_snapshot("web-0")
            snap.events = [
                Event(ts_utc="2026-01-01T00:00:00Z", name="obj-a",
                      reason="Killing", message="first message", type="Warning"),
                Event(ts_utc="2026-01-01T00:00:05Z", name="obj-b",
                      reason="Pulled", message="second message"),
            ]
            app._apply_snapshot(snap)
            await pilot.pause()

            et = app.query_one("#events_table", DataTable)
            row_key = et.coordinate_to_cell_key((0, 0)).row_key
            assert str(row_key.value).startswith("ev:2026-01-01T00:00:00Z|obj-a|")

            # the snapshot moves on WITHOUT a re-render: the click must open the
            # event that is actually DRAWN in that row, not a re-sorted guess
            moved = _pod_snapshot("web-0")
            moved.events = [
                Event(ts_utc="2026-01-01T00:01:00Z", name="obj-z",
                      reason="Unhealthy", message="newer message",
                      type="Warning"),
            ]
            app.snapshot = moved

            pushed: "list[object]" = []
            app.push_screen = lambda screen: pushed.append(screen)  # type: ignore

            app.on_data_table_row_selected(DataTable.RowSelected(et, 0, row_key))
            assert len(pushed) == 1
            assert pushed[0]._message == "first message"
            assert pushed[0]._name == "obj-a"
            await pilot.exit(None)

    asyncio.run(drive())
