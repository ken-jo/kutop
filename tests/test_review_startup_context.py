"""Regression tests for the two live-run defects reported after the review:

* a CONTEXT pick in the sidebar Select was dropped when a sidebar sync (the
  focus change from the closing dropdown) landed between the pick and its
  dispatch — the switch only "took" on the second attempt;
* launching with no kubectl current-context stacked "refresh failed" toasts
  full of klog noise (``E0902 … memcache.go:265] …``) instead of saying that no
  context is selected.

Plus the opt-in ``--log-file`` / ``KUTOP_LOG_FILE`` debug log added for
exactly this kind of diagnosis.
"""

from __future__ import annotations

import asyncio
import logging
import os

from textual.widgets import DataTable, Select

from kutop.fetch import clean_kubectl_error
from kutop.model import Snapshot
from kutop.render.app import TopApp
from kutop.render.sidebar import SidebarPanel

_KLOG = ('E0902 15:28:25.409307 13950 memcache.go:265] "Unhandled Error" '
         'err="couldn\'t get current server API group list: Get '
         '\\"http://localhost:8080/api?timeout=32s\\": dial tcp [::1]:8080: '
         'connect: connection refused"')
_SUMMARY = ("The connection to the server localhost:8080 was refused - did you "
            "specify the right host or port?")


# ── 1. a sidebar CONTEXT pick survives a concurrent sidebar sync ─────────────


def test_context_pick_is_not_dropped_by_a_concurrent_sidebar_sync() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="ctx-a")
        picked: "list[str]" = []
        app.set_context = lambda name: picked.append(name)  # type: ignore[method-assign]
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            sidebar.rebuild_contexts(["ctx-a", "ctx-b"], "ctx-a")
            await pilot.pause()
            await pilot.pause()
            sel = app.query_one("#side_context", Select)
            # the user picks ctx-b: the widget posts Select.Changed (queued) ...
            sel.value = "ctx-b"
            # ... and the closing dropdown moves focus, which re-syncs the
            # sidebar BEFORE that Changed is dispatched
            app._sync_sidebar_state()
            for _ in range(3):
                await pilot.pause()
            assert picked == ["ctx-b"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_programmatic_context_write_still_posts_no_pick() -> None:
    """The prevent() on programmatic writes is what keeps the sync from
    re-entering set_context — that guard must still hold without _syncing."""

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="ctx-a")
        picked: "list[str]" = []
        app.set_context = lambda name: picked.append(name)  # type: ignore[method-assign]
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            sidebar.rebuild_contexts(["ctx-a", "ctx-b"], "ctx-a")
            await pilot.pause()
            app.context = "ctx-b"
            app._sync_sidebar_state()          # programmatic: must not re-enter
            for _ in range(3):
                await pilot.pause()
            assert picked == []
            assert app.query_one("#side_context", Select).value == "ctx-b"
            await pilot.exit(None)

    asyncio.run(drive())


# ── 2. kubectl stderr is reduced to the human line ───────────────────────────


def test_clean_kubectl_error_prefers_kubectls_own_summary_line() -> None:
    raw = "\n".join([_KLOG, _KLOG, _KLOG, _SUMMARY])
    assert clean_kubectl_error(raw) == _SUMMARY


def test_clean_kubectl_error_unwraps_klog_only_output() -> None:
    cleaned = clean_kubectl_error(_KLOG)
    assert cleaned.startswith("couldn't get current server API group list")
    assert "memcache.go" not in cleaned
    assert 'Get "http://localhost:8080/api?timeout=32s"' in cleaned


def test_clean_kubectl_error_leaves_plain_errors_alone() -> None:
    msg = 'Error from server (Forbidden): pods is forbidden: User "x" cannot list'
    assert clean_kubectl_error(msg) == msg
    assert clean_kubectl_error("") == ""


# ── 3. startup with no current-context ───────────────────────────────────────


def _no_context_failure() -> Snapshot:
    snap = Snapshot()
    snap.errors = [f"get events -n default: {_SUMMARY}",
                   f"get nodes -o: {_SUMMARY}",
                   f"get pods -n default: {_SUMMARY}"]
    snap.error = snap.errors[0]
    return snap


def test_no_context_startup_shows_guidance_not_failure_toasts() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        notices: "list[str]" = []
        app.notify = lambda msg, **kw: notices.append(str(msg))  # type: ignore[assignment]
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            # core cycle, then the heavy cycle adds more failing sources
            app._apply_snapshot(_no_context_failure(), gen=app._fetch_gen)
            await pilot.pause()
            app._apply_snapshot(_no_context_failure(), gen=app._fetch_gen)
            await pilot.pause()

            assert not any(n.startswith("refresh failed") for n in notices)
            mt = app.query_one("#main_table", DataTable)
            rows = [str(mt.get_row_at(i)[0]) for i in range(mt.row_count)]
            assert rows[0].startswith("no kube context selected")
            assert "CONTEXT" in rows[1] and "kubectl config use-context" in rows[1]
            assert not any("cluster unreachable" in r for r in rows)
            await pilot.exit(None)

    asyncio.run(drive())


def test_real_outage_before_first_frame_keeps_the_unreachable_wording() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False,
                     context="prod")
        notices: "list[str]" = []
        app.notify = lambda msg, **kw: notices.append(str(msg))  # type: ignore[assignment]
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            snap = Snapshot()
            snap.error = "get nodes -o: dial tcp 10.0.0.1:6443: i/o timeout"
            snap.errors = [snap.error]
            app._apply_snapshot(snap, gen=app._fetch_gen)
            await pilot.pause()
            mt = app.query_one("#main_table", DataTable)
            first = str(mt.get_row_at(0)[0])
            assert first.startswith("cluster unreachable (context: prod)")
            # still no toast before the first frame: the rows carry the detail
            assert not any(n.startswith("refresh failed") for n in notices)
            await pilot.exit(None)

    asyncio.run(drive())


def test_full_failure_after_first_frame_still_toasts() -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        notices: "list[str]" = []
        app.notify = lambda msg, **kw: notices.append(str(msg))  # type: ignore[assignment]
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            from kutop.model import Node
            good = Snapshot()
            good.nodes = [Node(name="n1", ready=True)]
            app._apply_snapshot(good, gen=app._fetch_gen)
            await pilot.pause()
            snap = Snapshot()
            snap.error = "nodes: cluster down"
            snap.errors = [snap.error]
            app._apply_snapshot(snap, gen=app._fetch_gen)
            await pilot.pause()
            assert any(n.startswith("refresh failed") for n in notices)
            await pilot.exit(None)

    asyncio.run(drive())


# ── 4. --log-file / KUTOP_LOG_FILE ───────────────────────────────────────────


def test_log_file_option_records_fetch_failures(tmp_path, monkeypatch) -> None:
    from kutop.cli import _setup_log_file
    from kutop.fetch import Fetcher

    logger = logging.getLogger("kutop")
    before = list(logger.handlers)
    target = tmp_path / "kutop.log"
    try:
        assert _setup_log_file(str(target)) == str(target)
        f = Fetcher(["default"])
        f._record_fetch_error("get nodes -o: boom")
        for h in logger.handlers:
            h.flush()
        text = target.read_text(encoding="utf-8")
        assert "kutop call failed" not in text  # sanity: the message shape below
        assert "kubectl call failed: get nodes -o: boom" in text
        assert "starting" in text
    finally:
        for h in list(logger.handlers):
            if h not in before:
                logger.removeHandler(h)
                h.close()


def test_log_file_env_var_and_unwritable_path(tmp_path, monkeypatch, capsys) -> None:
    from kutop.cli import _setup_log_file

    logger = logging.getLogger("kutop")
    before = list(logger.handlers)
    try:
        monkeypatch.delenv("KUTOP_LOG_FILE", raising=False)
        assert _setup_log_file(None) is None            # opt-in only
        monkeypatch.setenv("KUTOP_LOG_FILE", str(tmp_path / "env.log"))
        assert _setup_log_file(None) == str(tmp_path / "env.log")
        bad = os.path.join(str(tmp_path), "missing-dir", "x.log")
        assert _setup_log_file(bad) is None              # never raises
        assert "cannot open log file" in capsys.readouterr().err
    finally:
        for h in list(logger.handlers):
            if h not in before:
                logger.removeHandler(h)
                h.close()
