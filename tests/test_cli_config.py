from __future__ import annotations

import asyncio
import re
from pathlib import Path

from textual.binding import Binding

import kutop
from kutop.cli import _build_parser, _parse_size
from kutop.config import (
    Config,
    METRICS_RESOLUTION_SECS,
    Profile,
    REFRESH_INTERVAL_SECS,
    apply_detail_preset,
    load_config,
)


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


def test_cli_accepts_metrics_bootstrap_opt_out() -> None:
    args = _build_parser().parse_args(["--no-metrics-bootstrap"])

    assert args.no_metrics_bootstrap is True


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


def test_load_config_reads_saved_keys_panel(tmp_path: Path) -> None:
    user_config = tmp_path / "config.yaml"
    user_config.write_text("panels:\n  keys: false\n", encoding="utf-8")

    cfg = load_config(user_path=str(user_config))

    assert cfg.show_keys is False
    assert cfg.to_dict()["panels"]["keys"] is False


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


def test_dump_config_includes_keys_panel() -> None:
    from kutop.config import dump_config_yaml

    text = dump_config_yaml(Config(show_keys=False))

    assert "keys: false" in text


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

    # the refresh cadence is fixed: a file (interval: 9) or CLI (interval: 2)
    # value is intentionally ignored.
    assert cfg.interval == REFRESH_INTERVAL_SECS
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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


def test_trend_history_accepts_real_zero_samples() -> None:
    from collections import deque

    from kutop.render.app import TopApp

    hist = deque([40, 100], maxlen=120)

    TopApp._append_trend(hist, used=0, cap=6000)

    assert hist[-1] == 0


def test_namespace_change_resets_trend_history(monkeypatch) -> None:
    from kutop.render.app import TopApp

    monkeypatch.setattr("kutop.render.app.save_config", lambda cfg, path=None: "/tmp/kutop-config.yaml")

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(namespaces=["default"]),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.cpu_hist.extend([40, 41])
            app.mem_hist.extend([99, 100])
            app.refresh_snapshot = lambda: None  # type: ignore[method-assign]

            app.set_namespaces(["kube-system"])

            assert list(app.cpu_hist) == []
            assert list(app.mem_hist) == []
            await pilot.exit(None)

    asyncio.run(drive())


def test_metrics_indicator_is_fixed_and_left_of_clock() -> None:
    from textual.widgets._header import HeaderClock

    from kutop import __version__
    from kutop.render.app import MetricsIndicator, TopApp

    assert TopApp.TITLE == f"kutop v{__version__}"

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            ind = app.query_one("#metrics_indicator", MetricsIndicator)
            clock = app.query_one(HeaderClock)
            # same btop placement the old adjuster used: left of the header clock
            assert ind.region.x < clock.region.x
            # a read-only metrics-freshness readout: shows the metrics-server
            # scrape resolution, no clickable +/- adjuster.
            plain = ind.render().plain
            assert f"{METRICS_RESOLUTION_SECS:g}s" in plain
            assert "metrics" in plain
            assert "+" not in plain and "-" not in plain
            await pilot.exit(None)

    asyncio.run(drive())


def test_refresh_interval_is_fixed_and_not_adjustable() -> None:
    from kutop.render.app import REFRESH_INTERVAL_SECS as APP_REFRESH, TopApp

    # the cadence constant is the single source of truth, re-exported via app
    assert APP_REFRESH == REFRESH_INTERVAL_SECS

    # a legacy Config(interval=...) / interval kwarg can no longer move the cadence
    app = TopApp(
        ["default"],
        interval=2.0,
        config=Config(interval=2.0),
        discover_namespaces=False,
        auto_refresh=False,
    )
    assert app.interval == REFRESH_INTERVAL_SECS
    assert app.cfg.interval == REFRESH_INTERVAL_SECS

    # the +/- adjuster machinery is gone: no key bindings, no actions
    actions = {b.action for b in TopApp.BINDINGS if isinstance(b, Binding)}
    assert "interval_up" not in actions
    assert "interval_down" not in actions
    assert not hasattr(app, "action_interval_up")
    assert not hasattr(app, "action_interval_down")


def test_options_view_has_no_interval_stepper() -> None:
    from kutop.render.app import TopApp
    from kutop.render.widgets import OptionsModal

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, OptionsModal)
            # the interval stepper has been removed from the View tab entirely
            assert not list(modal.query("#opt_interval_value"))
            assert not list(modal.query("#opt_interval_up"))
            assert not list(modal.query("#opt_interval_down"))
            await pilot.exit(None)

    asyncio.run(drive())


def test_panels_show_setup_hint_when_unconfigured() -> None:
    from textual.widgets import DataTable

    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(
                show_alerts=True, show_health=True,
                alertmanager_url="", health_probes=[],
            ),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # the health panel mounts even without probes, so it can show a hint
            assert list(app.query("#health_panel"))
            # the alerts panel shows a setup hint row when no alertmanager_url
            app._render_alerts()
            await pilot.pause()
            at = app.query_one("#alerts_panel", DataTable)
            assert "alertmanager_url" in str(at.get_row_at(0)[0])
            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_keys_panel_shows_contextual_hints() -> None:
    from textual.widgets import Static

    from kutop.model import Pod, Snapshot
    from kutop.render.app import TopApp

    def plain(static: Static) -> str:
        rendered = static.render()
        return rendered.plain if hasattr(rendered, "plain") else str(rendered)

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(show_keys=True),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            title = app.query_one("#side_keys_title", Static)
            body = app.query_one("#side_keys_body", Static)
            assert "KEYS · DASHBOARD" in plain(title)
            assert "focus pod for keys" in plain(body)

            snap = Snapshot()
            snap.pods = [
                Pod(
                    name="api-0",
                    namespace="default",
                    node="node-a",
                    phase="Running",
                    ready="1/1",
                )
            ]
            app._apply_snapshot(snap)
            await pilot.pause()

            assert "KEYS · POD ROW" in plain(title)
            pod_keys = plain(body)
            assert "l" in pod_keys and "Logs" in pod_keys
            assert "d" in pod_keys and "Describe" in pod_keys
            assert "x" in pod_keys and "Delete disabled" in pod_keys

            app.action_search()
            await pilot.pause()
            assert "KEYS · SEARCH" in plain(title)
            search_keys = plain(body)
            assert "/" in search_keys and "Edit search" in search_keys
            assert "enter" in search_keys and "Keep filter" in search_keys
            assert "esc" in search_keys and "Clear" in search_keys

            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_keys_pod_row_hints_are_not_clipped_in_snapshot(tmp_path: Path) -> None:
    from kutop.model import Pod, Snapshot
    from kutop.render.app import TopApp

    out = tmp_path / "pod-row-keys.svg"

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(show_keys=True),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            snap = Snapshot()
            snap.pods = [
                Pod(
                    name="api-0",
                    namespace="default",
                    node="node-a",
                    phase="Running",
                    ready="1/1",
                )
            ]
            app._apply_snapshot(snap)
            await pilot.pause()
            app.save_screenshot(str(out))
            await pilot.exit(None)

    asyncio.run(drive())

    svg = out.read_text(encoding="utf-8")
    assert "KEYS" in svg
    assert "POD" in svg
    assert "Logs" in svg
    assert "Describe" in svg
    assert "Delete" in svg


def test_delete_is_gated_by_live_allow_delete_toggle() -> None:
    from textual.widgets import Checkbox

    from kutop.model import Pod, Snapshot
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        # default launch: destructive off (no --allow-destructive)
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.allow_destructive is False
            snap = Snapshot()
            snap.pods = [Pod(name="api-0", namespace="default", node="node-a",
                             phase="Running", ready="1/1")]
            app._apply_snapshot(snap)
            await pilot.pause()

            # the sidebar exposes the soft toggle, off by default
            chk = app.query_one("#chk_allow_delete", Checkbox)
            assert chk.value is False

            # toggle off -> 'x' is a no-op warning, no confirm popup
            app.action_delete_pod()
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmModal)

            # enabling the live toggle flips the gate AND syncs the checkbox
            app.set_allow_destructive(True)
            await pilot.pause()
            assert app.allow_destructive is True
            assert app.query_one("#chk_allow_delete", Checkbox).value is True

            # now 'x' on the focused pod pops the delete-confirm modal
            app.action_delete_pod()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.exit(None)

    asyncio.run(drive())


def test_allow_destructive_flag_seeds_toggle_on() -> None:
    from textual.widgets import Checkbox

    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(["default"], allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # the CLI flag only seeds the initial toggle state
            assert app.allow_destructive is True
            assert app.query_one("#chk_allow_delete", Checkbox).value is True
            await pilot.exit(None)

    asyncio.run(drive())


def test_list_profiles_includes_builtin_example() -> None:
    from kutop.config import list_profiles

    assert "example" in list_profiles()


def test_set_profile_switches_authoritatively(tmp_path: Path, monkeypatch) -> None:
    import kutop.config as kconfig
    from textual.widgets import Select

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    (tmp_path / "teststack.yaml").write_text(
        """
name: teststack
namespaces: [team-x, team-y]
timezone: UTC
ordering:
  - { prefix: edge-, weight: 5 }
thresholds:
  cpu_warn: 11
  cpu_crit: 22
alertmanager_url: /api/alerts
health_probes:
  - name: svc
    url: /api/health
    fields: { ready: "ready=(\\\\w+)" }
""".strip(),
        encoding="utf-8",
    )

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # the dropdown discovered the new profile at construction time
            assert "teststack" in app._profile_opts
            # don't shell out to kubectl when the switch changes namespaces
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]

            app.set_profile("teststack")
            await pilot.pause()

            # profile-authoritative: ordering, thresholds, tz, ns, probes applied
            assert app.profile.name == "teststack"
            assert app.profile.weight_for("edge-1") == 5
            assert app.cfg.profile_name == "teststack"
            assert (app.cfg.cpu_warn, app.cfg.cpu_crit) == (11, 22)
            assert app.cfg.timezone == "UTC"
            assert app.cfg.namespaces == ["team-x", "team-y"]
            assert app.cfg.alertmanager_url == "/api/alerts"
            assert app.cfg.health_probes == [
                {"name": "svc", "url": "/api/health",
                 "fields": {"ready": "ready=(\\w+)"}}
            ]
            # the fetcher was rewired and the dropdown reflects the live profile
            assert app.fetcher.namespaces == ["team-x", "team-y"]
            assert app.fetcher.alertmanager_url == "/api/alerts"
            assert app.query_one("#side_profile", Select).value == "teststack"
            await pilot.exit(None)

    asyncio.run(drive())


def test_profiles_by_context_yaml_roundtrip(tmp_path: Path) -> None:
    from kutop.config import dump_config_yaml, load_config

    # an EKS-ARN-style context key exercises the quoted-key YAML emission
    arn = "arn:aws:eks:us-east-1:123456789:cluster/prod"
    cfg = Config(remember_profile_per_context=True,
                 profiles_by_context={arn: "prod-stack"})
    p = tmp_path / "config.yaml"
    p.write_text(dump_config_yaml(cfg), encoding="utf-8")

    loaded = load_config(user_path=str(p))
    assert loaded.remember_profile_per_context is True
    assert loaded.profiles_by_context == {arn: "prod-stack"}


def test_remember_profile_persists_context_map(tmp_path: Path, monkeypatch) -> None:
    import kutop.config as kconfig
    from textual.widgets import Checkbox

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(kconfig, "CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "teststack.yaml").write_text(
        "name: teststack\nnamespaces: [team-x]\n", encoding="utf-8")

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-a",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]
            assert app._context_key() == "ctx-a"

            # enabling remember + switching profile records the choice per context
            app.set_remember_profile_per_context(True)
            app.set_profile("teststack")
            await pilot.pause()
            assert app.cfg.remember_profile_per_context is True
            assert app.cfg.profiles_by_context == {"ctx-a": "teststack"}
            assert app.query_one("#chk_remember_profile", Checkbox).value is True

            # the map was persisted to the (redirected) config file
            saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
            assert "ctx-a" in saved and "teststack" in saved

            # switching back to generic forgets this context's entry
            app.set_profile("generic")
            await pilot.pause()
            assert app.cfg.profiles_by_context == {}
            await pilot.exit(None)

    asyncio.run(drive())


def test_profile_owned_fields_not_persisted_and_profile_authoritative(
        tmp_path: Path) -> None:
    from kutop.config import dump_config_yaml, load_config, save_config

    cfgfile = tmp_path / "config.yaml"

    # A session on context A using profile "db": cfg carries db's materialized
    # namespaces/thresholds + the recall map + a UI pref (theme).
    db_session = Config(
        profile_name="db",
        namespaces=["database", "postgres"],
        cpu_warn=50, cpu_crit=70,
        theme="nord",
        remember_profile_per_context=True,
        profiles_by_context={"ctxA": "db"},
    )
    save_config(db_session, str(cfgfile))
    saved = cfgfile.read_text(encoding="utf-8")
    # profile-owned fields must NOT leak into the shared file...
    assert "database" not in saved and "postgres" not in saved
    assert "cpu_warn: 50" not in saved
    # ...but UI prefs and the recall metadata DO persist
    assert "nord" in saved
    assert "ctxA" in saved and "db" in saved

    # context B (no remembered entry) launches generic: must NOT inherit db's
    # namespaces/thresholds via the shared file
    generic = load_config(user_path=str(cfgfile))
    assert generic.namespaces == ["default"]
    assert (generic.cpu_warn, generic.cpu_crit) == (75, 90)
    assert generic.profile_name == "generic"
    assert generic.remember_profile_per_context is True
    assert generic.profiles_by_context == {"ctxA": "db"}

    # an authoritative profile (e.g. --profile web, or recall) wins over the file
    # for profile-owned fields, while UI prefs from the file still apply
    web = Profile(name="web", namespaces=["web", "frontend"],
                  cpu_warn=33, cpu_crit=44)
    loaded = load_config(profile=web, user_path=str(cfgfile),
                         profile_authoritative=True)
    assert loaded.namespaces == ["web", "frontend"]
    assert (loaded.cpu_warn, loaded.cpu_crit) == (33, 44)
    assert loaded.profile_name == "web"
    assert loaded.theme == "nord"
    # and a quoted/escaped context key round-trips intact
    assert "profiles_by_context" in dump_config_yaml(db_session)


def test_sidebar_keys_panel_can_be_hidden() -> None:
    from textual.widgets import Static

    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(show_keys=False),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            assert app.query_one("#side_keys_title", Static).has_class("-hidden")
            assert app.query_one("#side_keys_body", Static).has_class("-hidden")

            await pilot.exit(None)

    asyncio.run(drive())


def test_options_panels_tab_controls_keys_panel() -> None:
    from textual.widgets import Checkbox

    from kutop.render.app import TopApp
    from kutop.render.widgets import OptionsModal

    saved: list[dict] = []

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(show_keys=True),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            assert isinstance(app.screen, OptionsModal)
            modal = app.screen
            cb = modal.query_one("#opt_p_keys", Checkbox)
            assert cb.value is True

            app.apply_config = lambda cfg: saved.append(cfg.to_dict())  # type: ignore[method-assign]
            cb.value = False
            await pilot.pause()

            assert saved[-1]["panels"]["keys"] is False
            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_shows_resolved_kube_context_name() -> None:
    from textual.widgets import Static

    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # no explicit override -> show the resolved current-context, and for
            # a long EKS ARN show the trailing cluster name, not "current"
            app.context = None
            app._resolved_context = (
                "arn:aws:eks:ap-northeast-2:054865923942:cluster/spm-eks"
            )
            app._sync_sidebar_state()
            await pilot.pause()
            status = app.query_one("#side_status", Static).render().plain
            assert "spm-eks" in status
            assert "ctx=current" not in status
            # an explicit --context override takes precedence
            app.context = "staging"
            assert app._display_context() == "staging"
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
        lambda cfg, path=None: saves.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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

    monkeypatch.setattr("kutop.render.app.save_config", lambda cfg, path=None: "/tmp/kutop-config.yaml")

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


def test_q_quits_only_after_second_press(monkeypatch) -> None:
    from kutop.render.app import TopApp

    bindings = {
        (b.key, b.action) if isinstance(b, Binding) else (b[0], b[1])
        for b in TopApp.BINDINGS
    }
    assert ("q", "quit_hint") in bindings
    assert ("q", "quit") not in bindings
    assert ("ctrl+q", "quit") not in bindings

    notices: list[tuple[str, dict]] = []
    exits: list[bool] = []

    def fake_notify(self, message: str, **kwargs) -> None:
        notices.append((message, kwargs))

    def fake_exit(self) -> None:
        exits.append(True)

    monkeypatch.setattr(TopApp, "notify", fake_notify)
    monkeypatch.setattr(TopApp, "exit", fake_exit)

    app = TopApp(
        ["default"],
        config=Config(theme="textual-dark"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    app.action_quit_hint()
    assert notices == [
        (
            "Press q again to quit",
            {"title": "Quit?", "timeout": 4},
        )
    ]
    assert exits == []

    app.action_quit_hint()
    assert exits == [True]


def test_options_modal_theme_preview_enter_persists(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
