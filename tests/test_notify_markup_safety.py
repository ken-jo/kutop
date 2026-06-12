"""Regression: notification toasts must never be parsed as Textual markup.

A kubectl timeout surfaces ``subprocess.TimeoutExpired``, whose str dumps the
whole argv — ``Command '['kubectl', ..., '-o', 'json']' timed out``. Passed to
the default ``notify(markup=True)`` the unbalanced ``[`` raised MarkupError from
inside the compositor and crashed the entire app during layout. kutop's toasts
are plain status/error text, so TopApp.notify defaults markup off.
"""

from __future__ import annotations

import asyncio
import inspect


def test_topapp_notify_defaults_markup_off() -> None:
    from kutop.render.app import TopApp

    sig = inspect.signature(TopApp.notify)
    assert sig.parameters["markup"].default is False


# The faithful crash message: the events fetch argv carries
# --sort-by=.lastTimestamp, and that '=' INSIDE the '[...]' is what makes the
# Textual markup parser expect a value and raise "Expected markup value".
_CRASH_MSG = (
    "get events -n staging: Command '['kubectl', '--context', 'spm-prod', "
    "'get', 'events', '-n', 'staging', '--sort-by=.lastTimestamp', '-o', "
    "'json']' timed out after 6 seconds"
)


def test_crash_message_really_is_markup_hostile() -> None:
    """Guard against a vacuous regression test: the message MUST break markup
    parsing, otherwise 'no crash' proves nothing."""
    import pytest
    from textual.content import Content

    with pytest.raises(Exception):
        Content.from_markup(f"refresh degraded: {_CRASH_MSG}")
    # ...while plain (markup-off) content of the same text is always fine
    Content(f"refresh degraded: {_CRASH_MSG}")


def test_notify_forwards_markup_false_to_base(monkeypatch) -> None:
    """The real wiring: TopApp.notify must hand markup=False to App.notify so
    the Toast renders the message as plain Content (the branch that never calls
    the markup parser). Spying the base call is deterministic — headless
    run_test does not lay out toasts, so a 'no crash' app test is vacuous.
    """
    from textual.app import App

    from kutop.render.app import TopApp

    captured = {}

    def fake(self, message, *, title="", severity="information",
             timeout=None, markup=True):
        captured["message"] = message
        captured["markup"] = markup

    monkeypatch.setattr(App, "notify", fake)
    app = TopApp.__new__(TopApp)  # notify only forwards to super; no full init
    TopApp.notify(app, f"refresh degraded: {_CRASH_MSG}")

    assert captured["markup"] is False
    # the forwarded message is exactly the markup-hostile text; with markup off
    # the Toast builds it via plain Content (proven safe in the test above)
    assert "--sort-by=.lastTimestamp" in captured["message"]

    # an explicit caller can still opt back into markup
    captured.clear()
    TopApp.notify(app, "[b]bold[/b]", markup=True)
    assert captured["markup"] is True


def test_app_notify_runs_without_raising_on_bracket_text() -> None:
    """End-to-end smoke: surfacing the crash message through the live app must
    complete. (Does not by itself prove the fix — see the spy test — but guards
    against notify wiring regressions that raise eagerly.)"""
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(["staging"], context="spm-prod",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(139, 61)) as pilot:
            await pilot.pause()
            app._notify_refresh_error(_CRASH_MSG, full=False)
            await pilot.pause()
            await pilot.exit(None)

    asyncio.run(drive())


def test_kubectl_timeout_message_has_no_argv_brackets() -> None:
    """_run turns a TimeoutExpired into a concise, bracket-free message."""
    import subprocess

    from kutop.fetch import Fetcher

    class FakeProc:
        returncode = None

        def communicate(self, timeout=None):
            # first call (the real wait) times out; the bounded drain returns
            if timeout and timeout > 1:
                raise subprocess.TimeoutExpired(cmd=["kubectl", "get", "x"],
                                                timeout=timeout)
            return ("", "")

        def kill(self):
            pass

    f = Fetcher(["default"])
    # route Popen to the fake without touching the real kubectl binary
    import kutop.fetch as fetch_mod
    orig = fetch_mod.subprocess.Popen
    fetch_mod.subprocess.Popen = lambda *a, **k: FakeProc()
    try:
        msg = f._run_safe("get", "pods", "-n", "default", "-o", "json")
        assert msg == ""  # failure -> empty stdout
    finally:
        fetch_mod.subprocess.Popen = orig

    recorded = f._fetch_errors
    assert recorded, "the timeout should have been recorded"
    text = recorded[0]
    assert "timed out after" in text
    assert "[" not in text and "kubectl'," not in text  # no argv dump
