"""Regression tests for the render-layer review fixes.

Three themes:

* **Markup safety** — every string that carries cluster- or user-controlled text
  (event messages, namespaces in modal headers, confirm bodies, health-probe
  names) must reach Textual as a ``rich.text.Text``/plain Content, never as
  markup. Textual parses ``[...]`` during LAYOUT, so a bad string does not raise
  where it was written: it crashes the compositor a frame later.
* **Process lifetime** — the kubectl children the modals spawn must die when the
  modal goes away, including the unmount path (quitting with a modal open).
* **Geometry / input** — border gutters, click-vs-drag, wrap.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Checkbox, Input, Label, RichLog

from kutop.config import Config
from kutop.render.modals import (
    DescribeModal,
    EventDetailModal,
    LogViewerModal,
    YamlViewModal,
)
from kutop.render.sidebar import SidebarPanel, SidebarState
from kutop.render.table import ResizableDataTable
# OptionsModal comes from render/options.py; import it through its documented
# re-export so the widgets <-> options import cycle is entered from the widgets
# side (importing render.options first hits the partially-initialised module).
from kutop.render.widgets import ConfirmModal, OptionsModal, TrendGraph

# A real-world event message: the '[' opens a tag and '/data/pvc-1]' reads as an
# auto-close, so Text.from_markup / a markup Label raises MarkupError on it.
HOSTILE = "unable to mount volume [/data/pvc-1]"


def _visual_plain(widget) -> str:
    """Plain text of a Static/Label's resolved visual (post markup handling)."""
    return widget.visual.plain


def test_hostile_message_really_breaks_markup() -> None:
    """Guard against a vacuous suite: the fixture text MUST break the parser."""
    from textual.content import Content

    from textual.markup import MarkupError

    with pytest.raises(MarkupError):
        Content.from_markup(HOSTILE)
    Content(HOSTILE)  # ...while plain content of the same text is fine


# ── 1. EventDetailModal: event text is never markup ──────────────────────────


def test_event_detail_modal_renders_hostile_message_literally() -> None:
    class Host(App):
        pass

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.push_screen(
                EventDetailModal("pod/web-0[1]", "FailedMount", HOSTILE)
            )
            await pilot.pause()
            log = app.screen.query_one("#ev_content", RichLog)
            written = "\n".join(strip.text for strip in log.lines)
            assert HOSTILE in written
            assert "pod/web-0[1]" in written
            await pilot.exit(None)

    asyncio.run(drive())


def test_event_detail_field_keeps_brackets_and_styles_only_the_label() -> None:
    line = EventDetailModal._field("Message:\n", HOSTILE)
    assert line.plain == f"Message:\n{HOSTILE}"
    # the label keeps its accent; the cluster text is unstyled
    assert line.spans[0].style == "bold yellow"
    assert line.spans[0].end == len("Message:\n")


# ── 2. modal headers keep [namespace] ────────────────────────────────────────


class _FakeStdout:
    async def readline(self) -> bytes:
        return b""


class _FakeSpawned:
    """Enough of asyncio.subprocess.Process for the modals' mount paths."""

    def __init__(self) -> None:
        self.returncode = None
        self.stdout = _FakeStdout()

    async def communicate(self):
        return (b"", b"")

    async def wait(self):
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15


@pytest.fixture
def no_kubectl(monkeypatch):
    """Never shell out for real: the modals spawn kubectl from ``on_mount``.

    Overriding ``on_mount`` in a subclass does NOT work — Textual dispatches
    every ``on_mount`` found along the MRO, so the base one still runs and
    leaks a child process into the next test.
    """
    import kutop.render.modals as modals_mod

    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append(list(argv))
        return _FakeSpawned()

    monkeypatch.setattr(modals_mod.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


@pytest.mark.parametrize(
    "make_modal, hdr_id",
    [
        (lambda: LogViewerModal("web-0", "kube-system", 100, None), "#log_hdr"),
        (lambda: DescribeModal("web-0", "kube-system", None), "#desc_hdr"),
        (lambda: YamlViewModal("web-0", "kube-system", None), "#desc_hdr"),
    ],
    ids=["logs", "describe", "yaml"],
)
def test_modal_header_keeps_namespace_brackets(make_modal, hdr_id, no_kubectl) -> None:
    class Host(App):
        pass

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.push_screen(make_modal())
            await pilot.pause()
            hdr = app.screen.query_one(hdr_id, Label)
            assert "[kube-system]" in _visual_plain(hdr)
            await pilot.exit(None)

    asyncio.run(drive())


def test_log_header_update_on_restart_is_not_markup() -> None:
    """_restart_stream rewrites the header; that write must stay literal too."""
    m = LogViewerModal("web-0", "kube-system", 100, None, containers=["a", "b"])
    assert "[kube-system]" in m._header_text()


# ── 3. kubectl children die on unmount ───────────────────────────────────────


class FakeProc:
    """Stand-in for asyncio.subprocess.Process: records terminate()/wait()."""

    def __init__(self, alive: bool = True, terminate_raises: bool = False) -> None:
        self.returncode = None if alive else 0
        self.terminated = 0
        self.waited = 0
        self._terminate_raises = terminate_raises

    def terminate(self) -> None:
        self.terminated += 1
        if self._terminate_raises:
            raise OSError("no such process")
        self.returncode = -15

    async def wait(self):
        self.waited += 1
        return self.returncode


def test_log_viewer_unmount_kills_kubectl() -> None:
    m = LogViewerModal("web-0", "default", 100, None)
    proc = FakeProc()
    m.proc = proc

    class FakeTask:
        cancelled = 0

        def cancel(self) -> None:
            type(self).cancelled += 1

    m.log_task = FakeTask()
    m.on_unmount()
    assert proc.terminated == 1, "quitting with the log modal open orphaned kubectl"
    assert FakeTask.cancelled == 1


def test_log_viewer_unmount_is_idempotent_after_close() -> None:
    m = LogViewerModal("web-0", "default", 100, None)
    proc = FakeProc()
    m.proc = proc
    m._teardown()
    m.on_unmount()          # second pass: process already reaped
    assert proc.terminated == 1


def test_describe_modal_unmount_kills_kubectl() -> None:
    m = DescribeModal("web-0", "default", None)
    proc = FakeProc()
    m._proc = proc
    m.on_unmount()
    assert proc.terminated == 1


def test_describe_modal_tracks_proc_attribute() -> None:
    assert DescribeModal("web-0", "default", None)._proc is None


def test_restart_stream_waits_before_replacing_proc() -> None:
    """The superseded process is terminated AND reaped before a new one starts,
    and the old task is awaited so its reader cannot outlive the switch."""
    m = LogViewerModal("web-0", "default", 100, None, containers=["a", "b"])
    old = FakeProc()
    m.proc = old
    seen: list = []

    async def fake_stream() -> None:
        # runs as the replacement task; record the state it starts from
        seen.append(("new_stream", old.terminated, old.waited, m.proc))

    async def drive() -> None:
        m._stream = fake_stream  # type: ignore[assignment]
        m.log_task = asyncio.ensure_future(asyncio.sleep(30))
        old_task = m.log_task
        await m._restart_stream()
        assert old_task.cancelled()
        await m.log_task

    asyncio.run(drive())
    assert old.terminated == 1 and old.waited == 1
    # the reference is only cleared once the process really exited
    assert seen == [("new_stream", 1, 1, None)]


def test_restart_stream_keeps_proc_when_terminate_fails() -> None:
    m = LogViewerModal("web-0", "default", 100, None)
    stuck = FakeProc(terminate_raises=True)
    m.proc = stuck

    async def drive() -> dict:
        held: dict = {}

        async def capture() -> None:
            held["proc"] = m.proc

        m._stream = capture  # type: ignore[assignment]
        await m._restart_stream()
        await m.log_task
        return held

    held = asyncio.run(drive())
    assert stuck.terminated == 1
    # terminate() blew up -> returncode stayed None -> reference NOT dropped
    assert held["proc"] is stuck


# ── 4. theme.tcss ────────────────────────────────────────────────────────────


def test_theme_centers_yaml_modal_and_sizes_the_keys_panel() -> None:
    from pathlib import Path

    import kutop.render as render_pkg

    css = (Path(render_pkg.__file__).parent / "theme.tcss").read_text()
    modal_rule = [ln for ln in css.splitlines() if "EventDetailModal {" in ln]
    assert modal_rule and "YamlViewModal" in modal_rule[0]
    # POD ROW ships 6 key rows; the box also carries the 1-row KEYS title
    assert "#side_keys_box {\n    height: 7;" in css
    assert "#side_keys_body {\n    width: 100%;\n    height: 6;" in css


def test_pod_row_key_context_fits_the_keys_panel() -> None:
    """The sizing above is only right if POD ROW really has 6 rows: the panel
    body must be tall enough for the widest key context the app can produce."""
    from kutop.model import Pod, Snapshot, Summary
    from kutop.render.app import TopApp

    async def drive() -> "tuple[str, int]":
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            snap = Snapshot(
                nodes=[],
                pods=[Pod(name="web-0", namespace="default", node="n1",
                          phase="Running", ready="1/1")],
                pvcs=[], events=[], alerts=[], health=[], summary=Summary(),
            )
            app._apply_snapshot(snap)
            await pilot.pause()
            app.query_one("#main_table").focus()
            await pilot.pause()
            title, rows = app._sidebar_key_context()
            await pilot.exit(None)
            return title, len(rows)

    title, count = asyncio.run(drive())
    assert title == "POD ROW", f"fixture did not reach the POD ROW context: {title}"
    assert count == 6


# ── 5. alertmanager input does not apply on every keystroke ──────────────────


def _options_app(cfg: Config):
    applied: list = []

    class Host(App):
        def compose(self) -> ComposeResult:
            yield Label("host")

        def apply_config(self, new_cfg) -> None:
            applied.append(new_cfg.alertmanager_url)

        def preview_theme(self, theme) -> None:
            pass

        def commit_theme(self, theme) -> None:
            pass

    return Host(), applied


def test_alertmanager_typing_does_not_apply_per_keystroke() -> None:
    cfg = Config()
    app, applied = _options_app(cfg)

    async def drive() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.push_screen(OptionsModal(cfg))
            await pilot.pause()
            await pilot.pause()
            inp = app.screen.query_one("#opt_alertmanager_url", Input)
            inp.focus()
            await pilot.pause()
            for ch in "http:":
                await pilot.press(ch)
            await pilot.pause()
            assert len(applied) <= 1, (
                f"typing 5 characters applied the config {len(applied)}x: {applied}"
            )
            assert applied == []
            # Enter commits once
            await pilot.press("enter")
            await pilot.pause()
            assert applied == ["http:"]
            await pilot.exit(None)

    asyncio.run(drive())


def test_alertmanager_commits_on_blur() -> None:
    cfg = Config()
    app, applied = _options_app(cfg)

    async def drive() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.push_screen(OptionsModal(cfg))
            await pilot.pause()
            await pilot.pause()
            inp = app.screen.query_one("#opt_alertmanager_url", Input)
            inp.focus()
            await pilot.pause()
            inp.value = "http://am/api/v2/alerts"
            await pilot.pause()
            assert applied == []
            app.screen.query_one("#opt_hp_name", Input).focus()
            await pilot.pause()
            assert applied == ["http://am/api/v2/alerts"]
            assert cfg.alertmanager_url == "http://am/api/v2/alerts"
            await pilot.exit(None)

    asyncio.run(drive())


# ── 6. health-probe rows are markup-safe ─────────────────────────────────────


def test_health_probe_with_brackets_renders_without_markup_error() -> None:
    cfg = Config()
    cfg.health_probes = [{"name": "a[/]b", "url": "http://x/[1]", "fields": {}}]
    app, _applied = _options_app(cfg)

    async def drive() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = OptionsModal(cfg)
            await app.push_screen(modal)
            # a MarkupError surfaces during layout, not at add_option time
            await pilot.pause()
            await pilot.pause()
            from textual.widgets import OptionList

            ol = app.screen.query_one("#opt_health_probes", OptionList)
            prompt = ol.get_option_at_index(0).prompt
            assert "a[/]b -> http://x/[1]" in getattr(prompt, "plain", str(prompt))
            # the notice line echoes the probe name -> must be literal too
            modal._hp_notice("removed a[/]b")
            await pilot.pause()
            notice = app.screen.query_one("#opt_hp_notice", Label)
            assert "a[/]b" in _visual_plain(notice)
            await pilot.exit(None)

    asyncio.run(drive())


# ── 7. ConfirmModal body is literal ──────────────────────────────────────────


def test_confirm_modal_body_renders_brackets_literally() -> None:
    class Host(App):
        pass

    body = "context: arn:aws:eks:[prod]\nnamespace: default\npod: web-0"

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.push_screen(ConfirmModal("DELETE POD", body, "Delete"))
            await pilot.pause()
            label = app.screen.query_one("#confirm_body", Label)
            assert "[prod]" in _visual_plain(label)
            await pilot.exit(None)

    asyncio.run(drive())


# ── 8. sidebar ───────────────────────────────────────────────────────────────


def test_rebuild_namespaces_never_exposes_old_plus_new() -> None:
    class Host(App):
        def compose(self) -> ComposeResult:
            yield SidebarPanel(
                ["alpha", "beta"],
                SidebarState(selected=["alpha", "beta"]),
                id="sidebar",
            )

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            assert sidebar.ns_checkbox_state() == ["alpha", "beta"]
            sidebar.rebuild_namespaces(["gamma"], ["gamma"])
            # read in the swap window: old OR new, never a union
            mid = sidebar.ns_checkbox_state()
            assert mid in (["alpha", "beta"], ["gamma"]), mid
            await pilot.pause()
            await pilot.pause()
            assert sidebar.ns_checkbox_state() == ["gamma"]
            assert len(
                sidebar.query_one("#side_ns_box", VerticalScroll).query(Checkbox)
            ) == 1
            await pilot.exit(None)

    asyncio.run(drive())


def test_programmatic_sort_select_write_emits_no_changed_echo() -> None:
    class Host(App):
        def compose(self) -> ComposeResult:
            yield SidebarPanel(["default"], SidebarState(selected=["default"]),
                               profile_options=["generic", "other"], id="sidebar")

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            seen: list = []
            sidebar.on_select_changed = lambda event: seen.append(event)  # type: ignore
            sidebar._set_select("side_sort", "cpu")
            sidebar._set_select("side_profile", "other")
            await pilot.pause()
            await pilot.pause()
            assert seen == [], "programmatic Select write leaked a Changed echo"
            await pilot.exit(None)

    asyncio.run(drive())


def test_select_changed_ignores_unchanged_value() -> None:
    """A Changed echo carrying the CURRENT value must not re-enter the app."""

    class FakeApp:
        calls: list = []

        def set_sort_key(self, key):
            self.calls.append(("sort", key))

        def set_profile(self, name):
            self.calls.append(("profile", name))

    class Event:
        def __init__(self, sid, value):
            self.select = type("S", (), {"id": sid})()
            self.value = value

    fake = FakeApp()

    class DetachedPanel(SidebarPanel):
        """SidebarPanel with the app seam replaced (no running message pump)."""

        @property
        def app(self):  # type: ignore[override]
            return fake

    panel = DetachedPanel(["default"], SidebarState(selected=["default"],
                                                    sort_key="cpu",
                                                    profile_name="prod"))
    panel._ready_for_input = True
    panel.on_select_changed(Event("side_sort", "cpu"))
    panel.on_select_changed(Event("side_profile", "prod"))
    assert fake.calls == []
    panel.on_select_changed(Event("side_sort", "mem"))
    assert fake.calls == [("sort", "mem")]


def test_render_keys_panel_survives_missing_widgets() -> None:
    """update_state must not abort when the keys widgets are not (yet) mounted."""
    panel = SidebarPanel(["default"], SidebarState(selected=["default"]))
    panel._render_keys_panel()  # unmounted -> NoMatches, swallowed


# ── 9. table geometry + click-vs-drag ────────────────────────────────────────


def test_content_x_subtracts_the_border_gutter() -> None:
    class Host(App):
        CSS = "#t { border: solid red; }"

        def compose(self) -> ComposeResult:
            yield ResizableDataTable(id="t")

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#t", ResizableDataTable)
            table.add_column("NODE/POD", width=20)
            table.add_row("x")
            await pilot.pause()
            gutter = int(table.content_region.x - table.region.x)
            assert gutter == 1, "the fixture needs a real border to be meaningful"
            assert table._content_x(10) == 10 - gutter + int(table.scroll_x)
            await pilot.exit(None)

    asyncio.run(drive())


def test_click_without_move_does_not_commit_a_width() -> None:
    committed: list = []

    class Host(App):
        cfg = Config()

        def compose(self) -> ComposeResult:
            yield ResizableDataTable(id="t")

        def commit_name_width(self, width) -> None:
            committed.append(width)

    class FakeEvent:
        def __init__(self, x: int) -> None:
            self.x = x

        def stop(self) -> None:
            pass

        def prevent_default(self) -> None:
            pass

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#t", ResizableDataTable)
            table.add_column("NODE/POD", width=20)
            table.add_row("x")
            await pilot.pause()
            boundary = table._resize_boundary_x()
            assert boundary is not None
            gutter = int(table.content_region.x - table.region.x)
            widget_x = boundary + gutter - int(table.scroll_x)

            # press + release with no move -> nothing persisted
            table.on_mouse_down(FakeEvent(widget_x))
            assert table._resizing
            table.on_mouse_up(FakeEvent(widget_x))
            assert committed == []

            # press + move + release -> committed once
            table.on_mouse_down(FakeEvent(widget_x))
            table.on_mouse_move(FakeEvent(widget_x + 6))
            table.on_mouse_up(FakeEvent(widget_x + 6))
            assert len(committed) == 1
            await pilot.exit(None)

    asyncio.run(drive())


# ── 10. TrendGraph never wraps ───────────────────────────────────────────────


@pytest.mark.parametrize("width", list(range(10, 19)))
def test_trend_meter_keeps_all_rows_at_narrow_widths(width: int) -> None:
    from rich.console import Console

    graph = TrendGraph("CPU", "cpu")
    text = graph._meter([10, 50, 90] * 8, 97, "12/16 cores", width)
    assert text.no_wrap is True
    console = Console(width=width, legacy_windows=False)
    lines = console.render_lines(text, console.options.update_width(width))
    # 1 "now" line + 3 heat rows; a wrap here pushes the bottom heat row out
    assert len(lines) == 1 + TrendGraph._HEAT_ROWS, (
        f"width={width} produced {len(lines)} rows"
    )
