"""Refresh-error diagnostics in the renderer.

Covers: a refresh that fails across several sources must toast EVERY broken
source (aggregated, capped, '+N more'), not just the first; a single failure
keeps the historical one-line toast; the once-per-distinct-text dedup is
preserved; delete/restart failure toasts no longer truncate kubectl stderr to
uselessness (200-char cap, whitespace collapsed).
"""

from __future__ import annotations

import asyncio

from kutop.model import Pod, Snapshot
from kutop.render.app import TopApp


def _degraded_snapshot(error: str, errors: list) -> Snapshot:
    """A partial-failure snapshot: one healthy pod plus the recorded errors."""
    snap = Snapshot()
    snap.pods = [Pod(name="web-0", namespace="good", node="node-a",
                     phase="Running", ready="1/1")]
    snap.error = error
    snap.errors = list(errors)
    return snap


def test_multi_source_failure_toast_names_each_source() -> None:
    async def drive() -> None:
        app = TopApp(["good", "team-a", "team-b"],
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices: list = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append((str(msg), kw.get("severity"))))

            errs = ["get pods -n team-a: forbidden",
                    "get pvc -n team-b: dial tcp: i/o timeout"]
            app._apply_snapshot(_degraded_snapshot(errs[0], errs))
            await pilot.pause()
            assert notices and notices[0][1] == "warning"
            msg = notices[0][0]
            assert msg.startswith("refresh degraded: 2 failures:")
            assert "team-a" in msg and "forbidden" in msg
            assert "team-b" in msg and "timeout" in msg

            # dedup contract: the identical failure set next tick stays silent
            app._apply_snapshot(_degraded_snapshot(errs[0], errs))
            await pilot.pause()
            assert len(notices) == 1

            # a FULL multi-source failure aggregates the same way, as an error
            snap = Snapshot()
            snap.error = "nodes: cluster down"
            snap.errors = ["nodes: cluster down", "pods: cluster down"]
            app._apply_snapshot(snap)
            await pilot.pause()
            assert notices[1][1] == "error"
            assert notices[1][0].startswith("refresh failed: 2 failures:")
            assert "nodes" in notices[1][0] and "pods" in notices[1][0]
            await pilot.exit(None)

    asyncio.run(drive())


def test_single_failure_toast_shape_unchanged() -> None:
    async def drive() -> None:
        app = TopApp(["team-a"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices: list = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append((str(msg), kw.get("severity"))))

            err = "get pods -n team-a: forbidden"
            app._apply_snapshot(_degraded_snapshot(err, [err]))
            await pilot.pause()
            # one failure: no '2 failures:' framing, the historical shape
            assert notices == [(f"refresh degraded: {err}", "warning")]
            await pilot.exit(None)

    asyncio.run(drive())


def test_more_than_three_failures_collapse_to_plus_n() -> None:
    app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
    notices: list = []
    app.notify = (  # type: ignore[assignment]
        lambda msg, **kw: notices.append(str(msg)))

    errs = [f"get pods -n team-{i}: forbidden" for i in range(5)]
    app._notify_refresh_error(errs[0], full=False, errors=errs)
    assert len(notices) == 1
    msg = notices[0]
    assert "5 failures:" in msg
    assert "+2 more" in msg
    # only the first three sources are spelled out
    assert "team-0" in msg and "team-1" in msg and "team-2" in msg
    assert "team-3" not in msg and "team-4" not in msg


def test_delete_failure_shows_150_char_stderr_untruncated(monkeypatch) -> None:
    """The old 80-char cap routinely cut the actual kubectl reason; a 150-char
    stderr must now reach the toast verbatim (whitespace already collapsed)."""
    import asyncio as _asyncio

    base = "Error from server (Forbidden): pods web-0 is forbidden: "
    stderr_150 = base + "x" * (150 - len(base))
    assert len(stderr_150) == 150

    class FailProc:
        returncode = 1

        async def communicate(self):
            return b"", stderr_150.encode()

    async def fake_exec(*argv, stdout=None, stderr=None):
        return FailProc()

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-b", allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        app.refresh_snapshot = lambda: None  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)
            notices: list = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append((str(msg), kw.get("severity", ""))))
            app._request_refresh = lambda: None  # type: ignore[assignment]

            app._do_delete_pod("web-0", "default")
            for _ in range(20):
                await pilot.pause(0.05)
                if notices:
                    break
            await pilot.pause()

            assert notices, "notify was not called on failure"
            assert notices[0] == (f"delete failed: {stderr_150}", "error")
            await pilot.exit(None)

    asyncio.run(drive())
