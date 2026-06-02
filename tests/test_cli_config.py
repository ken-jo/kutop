from __future__ import annotations

import asyncio
import re
from pathlib import Path

from textual.binding import Binding

import kutop
from kutop.cli import _build_parser, _parse_size
from kutop.config import Config, Profile, apply_detail_preset, load_config


def test_version_metadata_matches_package() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    assert match.group(1) == kutop.__version__


def test_cli_accepts_snapshot_view_and_size() -> None:
    args = _build_parser().parse_args(
        [
            "--snapshot",
            "out.svg",
            "--snapshot-view",
            "options-profile",
            "--size",
            "96x30",
        ]
    )
    assert args.snapshot == "out.svg"
    assert args.snapshot_view == "options-profile"
    assert _parse_size(args.size) == (96, 30)
    assert _parse_size("bad") == (140, 40)
    assert _parse_size("5x3") == (20, 10)


def test_cli_accepts_theme_override() -> None:
    args = _build_parser().parse_args(["--theme", "nord"])

    assert args.theme == "nord"


def test_detail_presets_adjust_columns_and_panels() -> None:
    wide = apply_detail_preset(Config(), "wide")
    assert wide.summary_style == "compact"
    assert wide.name_width == 20
    assert "namespace" in wide.columns

    full = apply_detail_preset(Config(), "full")
    assert full.show_pvc is True
    assert "storage_pct" in full.columns
    assert "owner_name" in full.columns


def test_summary_style_defaults_to_compact() -> None:
    assert Config().summary_style == "compact"


def test_invalid_summary_style_falls_back_to_compact(tmp_path: Path) -> None:
    user_config = tmp_path / "config.yaml"
    user_config.write_text("view:\n  summary_style: broken\n", encoding="utf-8")

    cfg = load_config(user_path=str(user_config))

    assert cfg.summary_style == "compact"


def test_load_config_reads_saved_theme(tmp_path: Path) -> None:
    user_config = tmp_path / "config.yaml"
    user_config.write_text(
        "view:\n  theme: nord\n  panel_backgrounds: false\n",
        encoding="utf-8",
    )

    cfg = load_config(user_path=str(user_config))

    assert cfg.theme == "nord"
    assert cfg.panel_backgrounds is False
    assert cfg.to_dict()["view"]["panel_backgrounds"] is False


def test_load_config_ignores_saved_name_filter_but_keeps_cli_filter(tmp_path: Path) -> None:
    user_config = tmp_path / "config.yaml"
    user_config.write_text(
        "filters:\n  name_filter: stale\n  hide_completed: true\n",
        encoding="utf-8",
    )

    cfg = load_config(user_path=str(user_config))

    assert cfg.name_filter == ""
    assert cfg.hide_completed is True

    cfg = load_config(
        user_path=str(user_config),
        cli_overrides={"filters": {"name_filter": "typed"}},
    )

    assert cfg.name_filter == "typed"


def test_dump_config_includes_panel_backgrounds() -> None:
    from kutop.config import dump_config_yaml

    text = dump_config_yaml(Config(panel_backgrounds=False))

    assert "panel_backgrounds: false" in text


def test_dump_config_does_not_persist_name_filter() -> None:
    from kutop.config import dump_config_yaml

    text = dump_config_yaml(Config(name_filter="stale"))

    assert 'name_filter: ""' in text
    assert "stale" not in text


def test_load_config_layers_profile_user_and_cli(tmp_path: Path) -> None:
    user_config = tmp_path / "config.yaml"
    user_config.write_text(
        """
view:
  interval: 9
  sort_key: cpu
cluster:
  namespaces: [user-ns]
panels:
  pvc: true
probes:
  health_probes:
    - name: api
      url: /api
      fields:
        ready: "ready=(\\\\w+)"
""".strip(),
        encoding="utf-8",
    )

    profile = Profile(
        namespaces=["profile-ns"],
        timezone="UTC",
        alertmanager_url="http://alerts.example/api/v2/alerts",
    )
    cfg = load_config(
        profile=profile,
        user_path=str(user_config),
        base_overrides={"cluster": {"namespaces": ["base-ns"]}},
        cli_overrides={
            "view": {"interval": 2},
            "cluster": {"namespaces": ["cli-ns"]},
        },
    )

    assert cfg.interval == 2
    assert cfg.namespaces == ["cli-ns"]
    assert cfg.timezone == "UTC"
    assert cfg.sort_key == "cpu"
    assert cfg.show_pvc is True
    assert cfg.alertmanager_url == "http://alerts.example/api/v2/alerts"
    assert cfg.health_probes == [
        {"name": "api", "url": "/api", "fields": {"ready": "ready=(\\w+)"}}
    ]


def test_app_applies_previews_and_persists_real_theme(monkeypatch) -> None:
    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            saved.clear()

            assert app.theme == "textual-dark"

            app.preview_theme("nord")
            assert app.theme == "nord"
            assert app.cfg.theme == "textual-dark"
            assert saved == []

            app.commit_theme("monokai")
            assert app.theme == "monokai"
            assert app.cfg.theme == "monokai"

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["view"]["theme"] == "monokai"


def test_app_applies_panel_background_theme_chrome(monkeypatch) -> None:
    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(panel_backgrounds=False),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            assert not app.has_class("-panel-backgrounds-on")
            assert not any(s.has_class("-panel-backgrounds-on") for s in app.screen_stack)

            app.apply_config(Config(panel_backgrounds=True))
            await pilot.pause()

            assert app.has_class("-panel-backgrounds-on")
            assert all(s.has_class("-panel-backgrounds-on") for s in app.screen_stack)

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["view"]["panel_backgrounds"] is True


def test_live_search_term_is_not_persisted(monkeypatch) -> None:
    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(name_filter="initial"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app._effective_filter() == "initial"
            assert app.cfg.name_filter == ""
            app._search_term = "typed-live"
            app.action_toggle_group()
            await pilot.pause()
            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["filters"]["name_filter"] == ""


def test_options_modal_toggles_panel_backgrounds(monkeypatch) -> None:
    from textual.widgets import Checkbox

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(panel_backgrounds=True),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            checkbox = app.screen.query_one("#opt_panel_backgrounds", Checkbox)
            checkbox.value = False
            await pilot.pause()

            assert app.cfg.panel_backgrounds is False
            assert not app.has_class("-panel-backgrounds-on")
            assert not any(s.has_class("-panel-backgrounds-on") for s in app.screen_stack)

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["view"]["panel_backgrounds"] is False


def test_panel_background_css_covers_datatable_layers() -> None:
    css = Path("kutop/render/theme.tcss").read_text(encoding="utf-8")

    assert "#search_bar {\n    height: 3;\n    layout: horizontal;\n    background: $surface;" in css
    assert "DataTable > .datatable--header {\n    background: $panel;" in css
    assert "Screen.-panel-backgrounds-on DataTable" in css
    assert "Screen.-panel-backgrounds-on DataTable > .datatable--even-row" in css
    assert "Screen.-panel-backgrounds-on DataTable > .datatable--fixed" in css
    assert "Screen.-panel-backgrounds-on .kpanel,\nScreen.-panel-backgrounds-on TrendGraph {\n    border-title-background: $surface;" in css
    assert ".kpanel-title" not in css
    assert ".kpanel-widget" not in css
    assert "sparkline--" not in css


def test_text_panel_uses_border_title_not_internal_title_row() -> None:
    from textual.app import App, ComposeResult

    from kutop.render.widgets import Panel

    class Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield Panel("CUSTOM HEALTH", id="health_panel", classes="-hidden")

    async def drive() -> None:
        app = Harness()
        async with app.run_test(size=(60, 10)) as pilot:
            await pilot.pause()

            panel = app.query_one("#health_panel", Panel)
            assert panel.border_title == "CUSTOM HEALTH"
            assert panel.has_class("kpanel")
            assert panel.has_class("-hidden")
            assert not list(panel.query(".kpanel-title"))
            assert panel.DEFAULT_CLASSES == "kpanel"

            await pilot.exit(None)

    asyncio.run(drive())


def test_preload_bottom_panels_hold_skeleton_area() -> None:
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(show_events=True, show_pvc=True),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            bottom = app.query_one("#bottom_box")
            events = app.query_one("#events_table")
            pvc = app.query_one("#pvc_table")

            assert bottom.region.height >= 6
            assert events.region.height == bottom.region.height
            assert pvc.region.height == bottom.region.height

            await pilot.exit(None)

    asyncio.run(drive())


def test_trend_graph_renders_thin_meter_canvas() -> None:
    from kutop.render.widgets import TrendGraph

    graph = TrendGraph("CPU OVERALL", "cpu")
    meter = graph._meter([10, 20, 30, 90], 30, "2.4/36", 32)
    lines = meter.plain.splitlines()

    assert len(lines) == 4
    assert lines[0].startswith("now")
    assert "2.4/36" in lines[0]
    assert "━" in lines[0]
    assert "─" in lines[0]
    assert lines[1].startswith("heat ")
    assert lines[2].startswith("     ")
    assert lines[3].startswith("     ")
    heat_cells = [line[5:] for line in lines[1:]]
    # bottom row is always filled; the curve rises toward the top on the peak
    assert any(ch in heat_cells[-1] for ch in "⣀⣤⣶⣿")
    assert any(ch in heat_cells[0] for ch in "⣀⣤⣶⣿")
    # area above the curve is left blank (spaces), not dotted
    assert " " in heat_cells[0]


def test_interval_indicator_sits_left_of_clock_and_tracks_value() -> None:
    from textual.widgets._header import HeaderClock

    from kutop.render.app import IntervalIndicator, TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(interval=2.0),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            ind = app.query_one("#interval_indicator", IntervalIndicator)
            clock = app.query_one(HeaderClock)
            # btop placement: docked to the LEFT of the header clock
            assert ind.region.x < clock.region.x
            # '-' and '+' flank the value (btop-style), spinner kept
            plain = ind.render().plain
            assert "⟳" in plain
            assert plain.index("-") < plain.index("2s") < plain.index("+")
            await pilot.exit(None)

    asyncio.run(drive())


def test_interval_nudge_keys_clamp_and_update() -> None:
    from kutop.render.app import IntervalIndicator, TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(interval=1.0),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # '-' at the 1.0s floor is a no-op
            app.action_interval_down()
            assert app.interval == 1.0
            # '+' adds 100 ms and the indicator + config follow
            app.action_interval_up()
            assert abs(app.interval - 1.1) < 1e-9
            assert abs(app.cfg.interval - 1.1) < 1e-9
            ind = app.query_one("#interval_indicator", IntervalIndicator)
            assert "1.1s" in ind.render().plain
            await pilot.exit(None)

    asyncio.run(drive())


def test_options_interval_is_stepper_and_syncs_app_and_header() -> None:
    from textual.widgets import Button, Static

    from kutop.render.app import IntervalIndicator, TopApp
    from kutop.render.widgets import OptionsModal

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(interval=2.0),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, OptionsModal)
            # interval is now a +/- stepper, not a free text Input
            assert list(modal.query("#opt_interval_value"))
            assert not list(modal.query("#opt_interval"))
            assert list(modal.query("#opt_interval_up"))
            assert list(modal.query("#opt_interval_down"))

            # press '+' once: 2.0 -> 2.1, and the change reaches the app + header
            modal.query_one("#opt_interval_up", Button).press()
            await pilot.pause()
            assert abs(app.cfg.interval - 2.1) < 1e-9
            assert abs(app.interval - 2.1) < 1e-9
            assert "2.1s" in modal.query_one("#opt_interval_value", Static).render().plain
            ind = app.query_one("#interval_indicator", IntervalIndicator)
            assert "2.1s" in ind.render().plain
            await pilot.exit(None)

    asyncio.run(drive())


def test_startup_does_not_persist_config(monkeypatch) -> None:
    """Launching must never rewrite the user config (data-loss regression).

    A startup autosave used to overwrite the loaded cfg back to disk, so any
    launch where load silently fell back to defaults (e.g. PyYAML missing) wiped
    the user's real settings. on_mount must not save; only user actions do.
    """
    from kutop.render.app import TopApp

    saves: list = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saves.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # startup (on_mount -> apply_panel_visibility) must NOT persist
            assert saves == []
            # a genuine user action (panel toggle) SHOULD persist
            app.action_toggle_events()
            await pilot.pause()
            assert len(saves) >= 1
            await pilot.exit(None)

    asyncio.run(drive())


def test_initial_refresh_applies_core_snapshot_before_enrichment() -> None:
    from kutop.model import Node, Pod, Snapshot
    from kutop.render.app import TopApp

    class FakeFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch_core(self) -> Snapshot:
            self.calls.append("core")
            snap = Snapshot()
            snap.nodes = [
                Node(name="node-a", cpu_mcpu=1, cpu_cap_mcpu=10, ready=True)
            ]
            snap.pods = [
                Pod(name="pod-a", namespace="default", node="node-a", phase="Running")
            ]
            return snap

        def enrich_snapshot(self, snap: Snapshot) -> Snapshot:
            self.calls.append("enrich")
            return snap

        def fetch(self) -> Snapshot:
            self.calls.append("full")
            return self.enrich_snapshot(self.fetch_core())

        def cancel(self) -> None:
            pass

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        fake = FakeFetcher()
        app.fetcher = fake  # type: ignore[assignment]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.refresh_snapshot()
            await pilot.pause()

            assert app._loaded is True
            assert fake.calls[:2] == ["core", "enrich"]

            await pilot.exit(None)

    asyncio.run(drive())


def test_header_hamburger_opens_kutop_menu() -> None:
    from kutop.render.app import ThemeHeaderIcon, TopApp

    from kutop.render.widgets import ThemeMenuModal

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click(ThemeHeaderIcon)
            await pilot.pause()

            assert isinstance(app.screen, ThemeMenuModal)

            await pilot.exit(None)

    asyncio.run(drive())


def test_theme_menu_has_native_actions_and_no_theme_rows(monkeypatch) -> None:
    from kutop.render.app import TopApp

    monkeypatch.setattr("kutop.render.app.save_config", lambda cfg: "/tmp/kutop-config.yaml")

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_theme_menu()
            await pilot.pause()

            from textual.widgets import OptionList
            menu = app.screen.query_one("#theme_menu_list", OptionList)
            option_ids = [opt.id for opt in menu.options]
            assert "action::keys" in option_ids
            assert "action::screenshot" in option_ids
            assert "action::quit" in option_ids
            assert "action::options" in option_ids
            assert not any(str(oid).startswith("theme::") for oid in option_ids)

            await pilot.exit(None)

    asyncio.run(drive())


def test_theme_menu_dismisses_on_outside_click() -> None:
    from kutop.render.app import TopApp
    from kutop.render.widgets import ThemeMenuModal

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_theme_menu()
            await pilot.pause()

            assert isinstance(app.screen, ThemeMenuModal)

            await pilot.click(offset=(70, 20))
            await pilot.pause()

            assert not isinstance(app.screen, ThemeMenuModal)

            await pilot.exit(None)

    asyncio.run(drive())


def test_q_shows_quit_hint_toast_and_ctrl_q_keeps_real_quit_binding(monkeypatch) -> None:
    from kutop.render.app import TopApp

    bindings = {
        (b.key, b.action) if isinstance(b, Binding) else (b[0], b[1])
        for b in TopApp.BINDINGS
    }
    assert ("q", "quit_hint") in bindings
    assert ("q", "quit") not in bindings
    assert ("ctrl+q", "quit") in bindings

    notices: list[tuple[str, dict]] = []

    def fake_notify(self, message: str, **kwargs) -> None:
        notices.append((message, kwargs))

    monkeypatch.setattr(TopApp, "notify", fake_notify)

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()

            await pilot.exit(None)

    asyncio.run(drive())

    assert notices == [
        (
            "Press Ctrl+Q to quit the app",
            {"title": "Quit", "timeout": 4},
        )
    ]


def test_options_modal_theme_preview_enter_persists(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            select = app.screen.query_one("#opt_theme", Select)
            select.focus()

            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert app.theme == "textual-light"
            assert app.cfg.theme == "textual-dark"
            assert saved == []

            await pilot.press("enter")
            await pilot.pause()
            assert app.theme == "textual-light"
            assert app.cfg.theme == "textual-light"

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["view"]["theme"] == "textual-light"


def test_options_modal_theme_escape_restores_without_persist(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            select = app.screen.query_one("#opt_theme", Select)
            select.focus()

            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert app.theme == "textual-dark"
            assert app.cfg.theme == "textual-dark"
            assert saved == []

            await pilot.exit(None)

    asyncio.run(drive())


def test_options_modal_theme_hover_previews(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp
    from kutop.render.widgets import ThemePreviewOverlay

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            select = app.screen.query_one("#opt_theme", Select)
            select.focus()
            await pilot.press("enter")
            await pilot.pause()

            overlay = select.query_one(ThemePreviewOverlay)
            overlay._mouse_hovering_over = 2
            await pilot.pause()

            assert app.theme == "nord"
            assert app.cfg.theme == "textual-dark"
            assert saved == []

            await pilot.exit(None)

    asyncio.run(drive())


def test_hidden_ansi_themes_are_not_offered() -> None:
    from kutop.render.app import TopApp

    app = TopApp(
        ["default"],
        config=Config(theme="textual-dark"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    assert "ansi-dark" not in app._theme_options()
    assert "ansi-light" not in app._theme_options()


def test_options_modal_context_input_persists(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(context="old-ctx"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app._discovered_contexts = ["old-ctx", "new-ctx"]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            context = app.screen.query_one("#opt_context", Select)
            context.focus()
            context.value = "new-ctx"
            await pilot.pause()

            assert app.cfg.context == "new-ctx"
            assert app.context == "new-ctx"
            assert app.fetcher.context == "new-ctx"

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["cluster"]["context"] == "new-ctx"


def test_options_modal_context_dropdown_discovers_kube_contexts(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    class Completed:
        returncode = 0
        stdout = "dev\nprod\n"

    monkeypatch.setattr(
        "kutop.render.app.subprocess.run",
        lambda *args, **kwargs: Completed(),
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(context="prod"),
            discover_namespaces=True,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()

            context = app.screen.query_one("#opt_context", Select)
            values = [value for _, value in context._options]

            assert "" in values
            assert "dev" in values
            assert "prod" in values
            assert context.value == "prod"

            await pilot.exit(None)

    asyncio.run(drive())


def test_app_falls_back_from_unknown_theme() -> None:
    from kutop.render.app import TopApp

    app = TopApp(
        ["default"],
        config=Config(theme="does-not-exist"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    assert app.theme == "textual-dark"
    assert app.cfg.theme == "textual-dark"


def test_app_falls_back_from_hidden_ansi_theme() -> None:
    from kutop.render.app import TopApp

    app = TopApp(
        ["default"],
        config=Config(theme="ansi-dark"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    assert app.theme == "textual-dark"
    assert app.cfg.theme == "textual-dark"
