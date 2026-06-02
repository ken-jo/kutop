from __future__ import annotations

import asyncio
import re
from pathlib import Path

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


def test_dump_config_includes_panel_backgrounds() -> None:
    from kutop.config import dump_config_yaml

    text = dump_config_yaml(Config(panel_backgrounds=False))

    assert "panel_backgrounds: false" in text


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

    bindings = {(key, action) for key, action, *_ in TopApp.BINDINGS}
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
