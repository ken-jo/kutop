from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from textual.binding import Binding

import kutop
from kutop.cli import _build_parser, _parse_size
from kutop.config import (
    Config,
    METRICS_RESOLUTION_SECS,
    Profile,
    REFRESH_INTERVAL_SECS,
    SNAPSHOT_DETAIL_LEVELS,
    SORTABLE_KEYS,
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


def test_cli_rejects_unknown_sort_key() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--sort", "bogus"])
    # every canonical sort key stays accepted
    for key in SORTABLE_KEYS:
        assert _build_parser().parse_args(["--sort", key]).sort == key


def test_cli_rejects_unknown_summary_style() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--summary-style", "bogus"])

    assert _build_parser().parse_args(["--summary-style", "tiles"]).summary_style == "tiles"


def test_cli_parses_profile_and_config_paths() -> None:
    args = _build_parser().parse_args(
        ["--profile", "teststack", "--config", "/tmp/alt-config.yaml"]
    )
    assert args.profile == "teststack"
    assert args.config == "/tmp/alt-config.yaml"

    # both default to None (config then falls back to ~/.config/kutop/config.yaml)
    defaults = _build_parser().parse_args([])
    assert defaults.profile is None
    assert defaults.config is None


def test_cli_parses_context_tz_and_filter_values() -> None:
    args = _build_parser().parse_args(
        ["--context", "prod", "--tz", "Asia/Seoul", "--filter", "api"]
    )
    assert args.context == "prod"
    assert args.tz == "Asia/Seoul"
    assert args.filter == "api"

    defaults = _build_parser().parse_args([])
    assert defaults.context is None
    assert defaults.tz is None
    assert defaults.filter is None


def test_cli_parses_boolean_view_flags() -> None:
    args = _build_parser().parse_args(
        ["--sort-desc", "--group-by-node", "--only-problems"]
    )
    assert args.sort_desc is True
    assert args.group_by_node is True
    assert args.only_problems is True

    defaults = _build_parser().parse_args([])
    assert defaults.sort_desc is False
    assert defaults.group_by_node is False
    assert defaults.only_problems is False


def test_cli_parses_log_tail_as_int() -> None:
    assert _build_parser().parse_args([]).log_tail == 150
    assert _build_parser().parse_args(["--log-tail", "500"]).log_tail == 500
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--log-tail", "many"])


def test_cli_rejects_unknown_detail_level() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--detail", "bogus"])
    # every documented preset stays accepted
    for level in SNAPSHOT_DETAIL_LEVELS:
        assert _build_parser().parse_args(["--detail", level]).detail == level
    assert _build_parser().parse_args([]).detail is None


def test_cli_parses_positional_namespaces_and_legacy_interval() -> None:
    args = _build_parser().parse_args(["team-a,team-b", "9"])
    assert args.namespaces == "team-a,team-b"
    assert args.interval == 9.0  # accepted (then ignored) for old top.sh calls

    defaults = _build_parser().parse_args([])
    assert defaults.namespaces is None
    assert defaults.interval is None


def test_main_exits_2_when_kubectl_missing(monkeypatch) -> None:
    import io

    from kutop import cli

    monkeypatch.setattr(cli.shutil, "which", lambda cmd: None)
    err = io.StringIO()
    monkeypatch.setattr(cli.sys, "stderr", err)

    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 2
    message = err.getvalue()
    assert "kubectl not found" in message
    assert "kubectl get nodes" in message
    assert "KUBECONFIG" in message


def test_self_test_still_runs_when_kubectl_missing(monkeypatch, tmp_path: Path) -> None:
    from kutop import cli

    # --self-test renders a synthetic frame and must stay kubectl-free: the
    # missing-kubectl gate only guards the live TUI path.
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: None)

    assert cli.main(["--self-test", "--config", str(tmp_path / "config.yaml")]) == 0


def test_main_wires_flags_and_namespaces_into_topapp(monkeypatch, tmp_path: Path) -> None:
    """cli.main builds TopApp from the layered config: positional namespaces,
    --context/--filter land in cfg, --allow-destructive/--log-tail pass through
    as constructor kwargs, and the metrics bootstrap runs on the chosen context."""
    from kutop import cli

    captured: list[dict] = []

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        def run(self) -> None:
            pass

    monkeypatch.setattr(cli.shutil, "which", lambda cmd: "/usr/bin/kubectl")
    # main imports TopApp lazily from kutop.render.app — patch that seam
    monkeypatch.setattr("kutop.render.app.TopApp", FakeApp)
    bootstraps: list = []
    monkeypatch.setattr(
        "kutop.metrics.maybe_bootstrap_metrics_server",
        lambda context=None: bootstraps.append(context),
    )

    assert cli.main([
        "team-a,team-b",
        "--config", str(tmp_path / "config.yaml"),
        "--context", "prod",
        "--filter", "api",
        "--allow-destructive",
        "--log-tail", "500",
    ]) == 0

    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["namespaces"] == ["team-a", "team-b"]
    assert kwargs["context"] == "prod"
    assert kwargs["allow_destructive"] is True
    assert kwargs["log_tail"] == 500
    assert kwargs["config_path"] == str(tmp_path / "config.yaml")
    cfg = kwargs["config"]
    assert cfg.namespaces == ["team-a", "team-b"]
    assert cfg.context == "prod"
    assert cfg.name_filter == "api"
    # live launch: discovery + auto refresh stay on, no legacy interval warning
    assert kwargs["discover_namespaces"] is True
    assert kwargs["auto_refresh"] is True
    assert kwargs["interval_deprecated"] is False
    assert bootstraps == ["prod"]


def test_live_main_emits_no_proxy_warning(
        monkeypatch, tmp_path: Path, capsys) -> None:
    """Proxy configuration is the user's own environment decision — kutop must
    not comment on it, on stderr or as an in-app warning. A proxy that really
    breaks a call surfaces as the kubectl error it causes."""
    from kutop import cli

    captured: list[dict] = []

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        def run(self) -> None:
            pass

    monkeypatch.setattr(cli.shutil, "which", lambda cmd: "/usr/bin/kubectl")
    monkeypatch.setattr("kutop.render.app.TopApp", FakeApp)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "")

    assert cli.main([
        "--no-metrics-bootstrap",
        "--config", str(tmp_path / "config.yaml"),
    ]) == 0

    assert capsys.readouterr().err == ""
    warnings = captured[0]["config"].load_warnings
    assert not [w for w in warnings if "PROXY" in w.upper()]
    assert not hasattr(cli, "_proxy_env_note")


def test_persist_state_surfaces_save_failure(monkeypatch) -> None:
    from kutop.render.app import TopApp

    app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
    seen: list[tuple[str, str]] = []

    def fail_save(*args, **kwargs) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr("kutop.render.app.save_config", fail_save)
    monkeypatch.setattr(
        app, "notify",
        lambda message, severity="information", **kwargs:
            seen.append((str(message), severity)),
    )

    app._persist_state()

    assert seen == [("config save failed: read-only filesystem", "error")]


def test_main_layers_profile_user_file_and_view_flags(monkeypatch, tmp_path: Path) -> None:
    """--profile loads authoritatively, the saved file still supplies UI prefs,
    and explicit view flags (--tz/--sort-desc/--group-by-node/--only-problems)
    reach load_config's cli_overrides on top of both."""
    import kutop.config as kconfig
    from kutop import cli

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    (tmp_path / "teststack.yaml").write_text(
        "name: teststack\nnamespaces: [team-x]\nthresholds: {cpu_warn: 11}\n",
        encoding="utf-8",
    )
    user_config = tmp_path / "config.yaml"
    user_config.write_text("view:\n  theme: nord\n", encoding="utf-8")

    captured: list[dict] = []

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        def run(self) -> None:
            pass

    monkeypatch.setattr(cli.shutil, "which", lambda cmd: "/usr/bin/kubectl")
    monkeypatch.setattr("kutop.render.app.TopApp", FakeApp)

    assert cli.main([
        "--profile", "teststack",
        "--config", str(user_config),
        "--tz", "Asia/Seoul",
        "--sort-desc",
        "--group-by-node",
        "--only-problems",
        "--no-metrics-bootstrap",
    ]) == 0

    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["profile"].name == "teststack"
    assert kwargs["namespaces"] == ["team-x"]
    cfg = kwargs["config"]
    # the profile is authoritative for the fields it supplies...
    assert cfg.profile_name == "teststack"
    assert cfg.namespaces == ["team-x"]
    assert cfg.cpu_warn == 11
    # ...the saved user file still contributes UI prefs...
    assert cfg.theme == "nord"
    # ...and explicit CLI flags layer on top of both
    assert cfg.timezone == "Asia/Seoul"
    assert cfg.sort_desc is True
    assert cfg.group_by_node is True
    assert cfg.only_problems is True


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
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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

    monkeypatch.setattr("kutop.render.app.save_config", lambda cfg, path=None, **kw: "/tmp/kutop-config.yaml")

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
            dash_keys = plain(body)
            assert "s" in dash_keys and "Sort" in dash_keys
            assert "/" in dash_keys and "Search" in dash_keys

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
            assert "X" in pod_keys and "Restart disabled" in pod_keys

            # the destructive gate flips both rows to their enabled labels
            app.set_allow_destructive(True)
            await pilot.pause()
            pod_keys = plain(body)
            assert "Delete" in pod_keys and "Delete disabled" not in pod_keys
            assert "Restart" in pod_keys and "Restart disabled" not in pod_keys
            app.set_allow_destructive(False)
            await pilot.pause()

            app.action_search()
            await pilot.pause()
            assert "KEYS · SEARCH" in plain(title)
            search_keys = plain(body)
            assert "/" in search_keys and "Edit search" in search_keys
            assert "enter" in search_keys and "Keep filter" in search_keys
            assert "esc" in search_keys and "Clear" in search_keys

            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_key_context_resolution() -> None:
    """_sidebar_key_context picks DASHBOARD/POD ROW/EVENTS/SEARCH from the
    live focus + search state, with rows sourced from the binding SOT."""
    from textual.widgets import DataTable, Static

    from kutop.model import Pod, Snapshot
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(show_keys=True),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # empty dashboard: a curated core set, not a placeholder
            context, rows = app._sidebar_key_context()
            assert context == "DASHBOARD"
            assert ("s", "Sort") in rows
            assert ("g", "Group") in rows
            assert ("/", "Search") in rows
            assert ("b", "Sidebar") in rows

            # a focused pod row exposes the pod verbs; Delete mirrors the gate
            snap = Snapshot()
            snap.pods = [Pod(name="api-0", namespace="default", node="node-a",
                             phase="Running", ready="1/1")]
            app._apply_snapshot(snap)
            await pilot.pause()
            context, rows = app._sidebar_key_context()
            assert context == "POD ROW"
            assert ("x", "Delete disabled") in rows
            app.set_allow_destructive(True)
            context, rows = app._sidebar_key_context()
            assert ("x", "Delete") in rows

            # focusing the warning-events table flips to EVENTS immediately,
            # even though the pod cursor is unchanged
            app.query_one("#events_table", DataTable).focus()
            await pilot.pause()
            context, rows = app._sidebar_key_context()
            assert context == "EVENTS"
            assert ("enter", "Details") in rows
            assert ("e", "Hide events") in rows
            title = app.query_one("#side_keys_title", Static)
            rendered = title.render()
            shown = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "KEYS · EVENTS" in shown  # synced on focus, no cursor move

            # a visible search bar outranks every other context
            app.action_search()
            await pilot.pause()
            context, _rows = app._sidebar_key_context()
            assert context == "SEARCH"

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


def test_set_profile_refreshes_even_without_namespace_change(
        tmp_path: Path, monkeypatch) -> None:
    import kutop.config as kconfig

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    # two profiles that watch the SAME namespace but differ in thresholds, so a
    # switch between them does NOT change the namespace set
    (tmp_path / "pa.yaml").write_text(
        "name: pa\nnamespaces: [shared]\nthresholds: {cpu_warn: 10}\n",
        encoding="utf-8")
    (tmp_path / "pb.yaml").write_text(
        "name: pb\nnamespaces: [shared]\nthresholds: {cpu_warn: 20}\n",
        encoding="utf-8")

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            calls: list = []
            app.refresh_snapshot = lambda: calls.append(1)  # type: ignore[assignment]

            app.set_profile("pa")          # default -> shared (namespace change)
            await pilot.pause()
            calls.clear()
            app.set_profile("pb")          # shared -> shared (NO namespace change)
            await pilot.pause()
            # a profile switch must still refresh so new thresholds/probes apply
            assert calls, "profile switch should refresh even without a ns change"
            assert app.cfg.cpu_warn == 20
            await pilot.exit(None)

    asyncio.run(drive())


def test_profile_context_is_authoritative(tmp_path: Path) -> None:
    from kutop.config import load_config, save_config

    cfgfile = tmp_path / "config.yaml"
    # a persisted (generic) file that pins the context to cluster-a
    save_config(Config(context="cluster-a"), str(cfgfile))

    # a profile that targets cluster-b, loaded authoritatively, wins over the file
    p = Profile(name="bundle", context="cluster-b", namespaces=["ns-b"])
    loaded = load_config(profile=p, user_path=str(cfgfile),
                         profile_authoritative=True)
    assert loaded.context == "cluster-b"
    assert loaded.namespaces == ["ns-b"]


def test_profile_switches_kube_context_on_select(tmp_path: Path, monkeypatch) -> None:
    import kutop.config as kconfig

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    (tmp_path / "onctx.yaml").write_text(
        "name: onctx\ncontext: cluster-b\nnamespaces: [appns]\n", encoding="utf-8")

    async def drive() -> None:
        app = TopApp(["default"], context="cluster-a",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]
            assert app.context == "cluster-a"

            # selecting a profile that pins a context switches the cluster
            app.set_profile("onctx")
            await pilot.pause()
            assert app.cfg.context == "cluster-b"
            assert app.context == "cluster-b"
            assert app.fetcher.context == "cluster-b"

            # a profile WITHOUT a context keeps the current cluster
            app.set_profile("generic")
            await pilot.pause()
            assert app.cfg.context == "cluster-b"
            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_context_dropdown_switches_cluster(tmp_path: Path, monkeypatch) -> None:
    import kutop.config as kconfig
    from textual.widgets import Select

    from kutop.render.app import TopApp

    # don't touch the real ~/.config/kutop/config.yaml on the persisting switch
    monkeypatch.setattr(kconfig, "CONFIG_PATH", str(tmp_path / "config.yaml"))

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-a",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]
            # the sidebar exposes a CONTEXT dropdown
            app.query_one("#side_context", Select)

            # selecting a context switches the live cluster + rewires the fetcher
            app.set_context("ctx-b")
            await pilot.pause()
            assert app.cfg.context == "ctx-b"
            assert app.context == "ctx-b"
            assert app.fetcher.context == "ctx-b"
            await pilot.exit(None)

    asyncio.run(drive())


def test_rebuild_contexts_handles_current_not_in_options_and_empty(
        tmp_path: Path, monkeypatch) -> None:
    """LIVE path: discovery refills the CONTEXT Select after mount.

    rebuild_contexts must never raise even when the active context is not in the
    discovered list (falls back to the first option) or when discovery returns an
    empty list, mirroring how a worker thread drives it via _populate_ns_list.
    """
    import kutop.config as kconfig
    from textual.widgets import Select

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "CONFIG_PATH", str(tmp_path / "config.yaml"))

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-a",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar")
            sel = app.query_one("#side_context", Select)

            # 1) current ("ctx-a") is NOT in the discovered options -> falls back
            #    to the first option without raising InvalidSelectValueError
            sidebar.rebuild_contexts(["ctx-b", "ctx-c"], "ctx-a")
            await pilot.pause()
            values = [value for _, value in sel._options]
            assert "ctx-b" in values and "ctx-c" in values
            assert sel.value == "ctx-b"

            # 2) current IS in the refreshed list -> stays selected
            sidebar.rebuild_contexts(["ctx-b", "ctx-c"], "ctx-c")
            await pilot.pause()
            assert sel.value == "ctx-c"

            # 3) empty discovery list must not raise and keeps a usable value
            sidebar.rebuild_contexts([], "")
            await pilot.pause()
            assert sel.value is not None

            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_context_pick_does_not_loop_or_crash(
        tmp_path: Path, monkeypatch) -> None:
    """LIVE path regression: a single CONTEXT pick must call set_context once.

    A programmatic Select rebuild posts a queued Changed echo; without the
    _syncing re-arm + idempotency guards this re-entered set_context in an
    unbounded loop ending in a NoMatches crash. The pick is driven through the
    real Select.Changed handler (sel.value = ...), exactly like a user click.
    """
    import kutop.config as kconfig
    from textual.widgets import Select

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "CONFIG_PATH", str(tmp_path / "config.yaml"))

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-a",
                     discover_namespaces=False, auto_refresh=False)
        calls: list[str] = []
        orig = TopApp.set_context

        def counting(self, name):  # type: ignore[no-untyped-def]
            calls.append(name)
            assert len(calls) <= 50, f"set_context looped: {calls[:8]}"
            return orig(self, name)

        app.set_context = counting.__get__(app, TopApp)  # type: ignore[assignment]

        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]
            sidebar = app.query_one("#sidebar")
            # discovery fills the dropdown after mount
            app._discovered_contexts = ["ctx-a", "ctx-b"]
            sidebar.rebuild_contexts(["ctx-a", "ctx-b"], "ctx-a")
            for _ in range(4):
                await pilot.pause()
            sidebar._ready_for_input = True

            # a user picks ctx-b in the dropdown (fires Select.Changed)
            sel = app.query_one("#side_context", Select)
            sel.value = "ctx-b"
            for _ in range(12):
                await pilot.pause()

            assert calls == ["ctx-b"], calls
            assert app.context == "ctx-b"
            assert sel.value == "ctx-b"
            await pilot.exit(None)

    asyncio.run(drive())


def test_adopt_config_and_set_context_survive_unmounted_widgets(
        tmp_path: Path, monkeypatch) -> None:
    """LIVE path regression: _adopt_config must not raise NoMatches when a panel
    widget is transiently absent (re-entered before first paint / during teardown).
    """
    import copy

    import kutop.config as kconfig

    from kutop.render.app import TopApp

    monkeypatch.setattr(kconfig, "CONFIG_PATH", str(tmp_path / "config.yaml"))

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-a",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]

            # simulate a transient un-mounted state and re-adopt the config
            app.query_one("#summary_bar").remove()
            await pilot.pause()
            cfg = copy.deepcopy(app.cfg)
            app._adopt_config(cfg, persist=False)  # must NOT raise NoMatches

            # set_context driving through the same path also stays crash-free
            app.set_context("ctx-b")
            await pilot.pause()
            assert app.context == "ctx-b"
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
    # profile-owned VALUES must NOT leak into the shared file...
    assert "database" not in saved and "postgres" not in saved
    assert "cpu_warn: 50" not in saved
    # ...but UI prefs, the recall metadata, AND the active profile name persist
    # (profile_name is now retained so the next launch can reload the profile,
    # which re-supplies the owned VALUES via the profile layer)
    assert "nord" in saved
    assert "ctxA" in saved and "db" in saved
    assert 'profile: "db"' in saved

    # reloading the (generic, no --profile) file keeps the recorded profile_name
    # while the profile-owned VALUES read back as the generic defaults (no leak):
    # the values were reset on persist; the name only flags which profile to
    # reload next launch (cli.main does that reload authoritatively).
    generic = load_config(user_path=str(cfgfile))
    assert generic.namespaces == ["default"]
    assert (generic.cpu_warn, generic.cpu_crit) == (75, 90)
    assert generic.profile_name == "db"
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


def test_last_profile_health_round_trips_through_persist_and_reload(
        tmp_path: Path, monkeypatch) -> None:
    """The reported data-loss bug: a profile's health probes survive a relaunch.

    Saving a Config whose profile is active drops the owned VALUES but keeps the
    profile NAME. Reloading the way ``cli.main`` does (load_profile(name) +
    load_config(profile_authoritative=True)) re-supplies namespaces + the health
    probes (with their fields) from the profile layer — so the health row that
    "stops appearing after using the app" comes back.
    """
    import kutop.config as kconfig
    from kutop.config import (
        HealthProbe,
        Profile,
        load_config,
        load_profile,
        save_config,
    )

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    (tmp_path / "arbitrum-l3.yaml").write_text(
        """
name: arbitrum-l3
namespaces: [l3-rollup, l3-batcher]
health_probes:
  - name: sequencer
    url: http://seq.local/health
    fields:
      block: 'block=(\\d+)'
      peers: 'peers=(\\d+)'
""".strip(),
        encoding="utf-8",
    )

    cfgfile = tmp_path / "config.yaml"

    # a session running the arbitrum-l3 profile: cfg carries the profile's
    # materialized namespaces + health probes (as cfg would after load).
    profile = load_profile("arbitrum-l3")
    session = load_config(profile=profile, user_path=str(cfgfile),
                          profile_authoritative=True)
    assert session.profile_name == "arbitrum-l3"
    assert session.namespaces == ["l3-rollup", "l3-batcher"]
    assert session.health_probes == [
        {"name": "sequencer", "url": "http://seq.local/health",
         "fields": {"block": "block=(\\d+)", "peers": "peers=(\\d+)"}}
    ]

    # persist it (as the running app does on a user action)
    save_config(session, str(cfgfile))
    saved = cfgfile.read_text(encoding="utf-8")
    # the saved file keeps the active profile NAME...
    assert 'profile: "arbitrum-l3"' in saved
    # ...but drops the profile-owned VALUES (namespaces + health probes)
    assert "l3-rollup" not in saved and "l3-batcher" not in saved
    assert "sequencer" not in saved and "seq.local" not in saved

    # a plain reload (no --profile) reads the file back: the owned VALUES are the
    # generic baseline, but the recorded profile_name flags which profile to reload
    plain = load_config(user_path=str(cfgfile))
    assert plain.profile_name == "arbitrum-l3"
    assert plain.namespaces == ["default"]
    assert plain.health_probes == []

    # cli.main reloads that profile authoritatively -> namespaces + health probes
    # (with their fields) are restored, so the health panel reappears.
    reloaded_profile = load_profile(plain.profile_name)
    assert isinstance(reloaded_profile, Profile)
    assert all(isinstance(hp, HealthProbe) for hp in reloaded_profile.health_probes)
    reloaded = load_config(profile=reloaded_profile, user_path=str(cfgfile),
                           profile_authoritative=True)
    assert reloaded.namespaces == ["l3-rollup", "l3-batcher"]
    assert reloaded.health_probes == [
        {"name": "sequencer", "url": "http://seq.local/health",
         "fields": {"block": "block=(\\d+)", "peers": "peers=(\\d+)"}}
    ]


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
        lambda cfg, path=None, **kw: saves.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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

        def enrich_snapshot(self, snap: Snapshot, *, heavy: bool = True) -> Snapshot:
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


def test_first_fetch_failure_shows_persistent_guidance_until_snapshot() -> None:
    from textual.widgets import DataTable

    from kutop.fetch import Fetcher
    from kutop.model import Node, Pod, Snapshot
    from kutop.render.app import TopApp

    class UnreachableFetcher(Fetcher):
        """Every kubectl call fails, as with a dead/unauthorized cluster."""

        def _run(self, *args: str, timeout: int = 0) -> str:
            raise RuntimeError("Unable to connect to the server: dial tcp: timeout")

    class GoodFetcher:
        def fetch_core(self) -> Snapshot:
            snap = Snapshot()
            snap.nodes = [
                Node(name="node-a", cpu_mcpu=1, cpu_cap_mcpu=10, ready=True)
            ]
            snap.pods = [
                Pod(name="pod-a", namespace="default", node="node-a",
                    phase="Running", ready="1/1")
            ]
            return snap

        def enrich_snapshot(self, snap: Snapshot, *, heavy: bool = True) -> Snapshot:
            return snap

        def fetch(self) -> Snapshot:
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
        app.fetcher = UnreachableFetcher(["default"])  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            def first_cells(mt: DataTable) -> list[str]:
                return [str(mt.get_row_at(i)[0]) for i in range(mt.row_count)]

            app.refresh_snapshot()
            for _ in range(100):
                await pilot.pause(0.02)
                if not app._fetching:
                    break
            await pilot.pause()

            # no snapshot ever applied: the bare Loading row is replaced by
            # persistent guidance instead of a 4s toast nobody can re-read
            assert app._loaded is False
            mt = app.query_one("#main_table", DataTable)
            rows = first_cells(mt)
            assert any("cluster unreachable" in r for r in rows)
            assert any("connect" in r for r in rows)
            assert any("check: kubectl get nodes" in r for r in rows)
            assert any("retrying every 5s" in r for r in rows)
            assert not any("Loading cluster snapshot" in r for r in rows)

            # a retry failing with DIFFERENT text updates the guidance rows
            changed = Snapshot()
            changed.error = "get nodes: certificate has expired"
            app._apply_snapshot(changed)
            await pilot.pause()
            rows = first_cells(mt)
            assert any("certificate has expired" in r for r in rows)

            # the first successful snapshot replaces the guidance with pods
            app.fetcher = GoodFetcher()  # type: ignore[assignment]
            app.refresh_snapshot()
            for _ in range(100):
                await pilot.pause(0.02)
                if app._loaded and not app._fetching:
                    break
            await pilot.pause()
            assert app._loaded is True
            rows = first_cells(mt)
            assert any("pod-a" in r for r in rows)
            assert not any("cluster unreachable" in r for r in rows)
            assert not any("kubectl get nodes" in r for r in rows)

            await pilot.exit(None)

    asyncio.run(drive())


def test_startup_guidance_survives_sort_and_column_changes() -> None:
    """Guidance rows shown during the first-fetch failure must persist across
    sort cycles and column rebuilds — the table must not become blank while
    the cluster is still unreachable."""
    from textual.widgets import DataTable

    from kutop.fetch import Fetcher
    from kutop.render.app import TopApp

    class UnreachableFetcher(Fetcher):
        def _run(self, *args: str, timeout: int = 0) -> str:
            raise RuntimeError("Unable to connect to the server: cluster unreachable")

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.fetcher = UnreachableFetcher(["default"])  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            def first_cells(mt: DataTable) -> list[str]:
                return [str(mt.get_row_at(i)[0]) for i in range(mt.row_count)]

            # trigger a fetch attempt so the guidance rows appear
            app.refresh_snapshot()
            for _ in range(100):
                await pilot.pause(0.02)
                if not app._fetching:
                    break
            await pilot.pause()

            mt = app.query_one("#main_table", DataTable)
            assert app._loaded is False
            rows = first_cells(mt)
            assert any("cluster unreachable" in r for r in rows)
            assert mt.row_count >= 3

            # cycle the sort key — triggers a column rebuild via _render_main_table
            app.action_cycle_sort()
            await pilot.pause()

            # guidance rows must still be present after the rebuild
            rows_after = first_cells(mt)
            assert mt.row_count >= 3
            assert any("cluster unreachable" in r for r in rows_after)

            await pilot.exit(None)

    asyncio.run(drive())


def test_header_hamburger_reveals_hidden_sidebar() -> None:
    from kutop.render.app import ThemeHeaderIcon, TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar")
            app.action_toggle_sidebar()
            assert sidebar.has_class("-hidden")

            await pilot.click(ThemeHeaderIcon)
            await pilot.pause()

            assert not sidebar.has_class("-hidden")

            await pilot.exit(None)

    asyncio.run(drive())


def test_header_hamburger_focuses_menu_when_sidebar_visible() -> None:
    from kutop.render.app import ThemeHeaderIcon, TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            sidebar = app.query_one("#sidebar")
            assert not sidebar.has_class("-hidden")

            await pilot.click(ThemeHeaderIcon)
            await pilot.pause()

            assert not sidebar.has_class("-hidden")
            assert app.focused is not None
            assert app.focused.id == "side_menu_options"

            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_menu_lists_native_actions() -> None:
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            from textual.widgets import Button
            menu_ids = [
                btn.id for btn in app.query_one("#sidebar").query(Button)
                if btn.has_class("side_menu_btn")
            ]
            assert menu_ids == [
                "side_menu_options",
                "side_menu_keys",
                "side_menu_screenshot",
                "side_menu_quit",
            ]

            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_menu_buttons_fire_app_actions(monkeypatch) -> None:
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        calls: list[str] = []
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(app, "action_open_options",
                                lambda: calls.append("options"))
            monkeypatch.setattr(app, "action_quit", lambda: calls.append("quit"))
            monkeypatch.setattr(app, "action_quit_hint",
                                lambda: calls.append("quit_hint"))

            async def fake_run_action(action, default_namespace=None):
                calls.append(str(action))
                return True

            monkeypatch.setattr(app, "run_action", fake_run_action)

            from textual.widgets import Button
            for bid in ("side_menu_options", "side_menu_keys",
                        "side_menu_screenshot", "side_menu_quit"):
                app.query_one(f"#{bid}", Button).press()
                await pilot.pause()

            # the menu Quit is an explicit selection: it must take the direct
            # quit path, never the two-step quit_hint flow
            assert calls == ["options", "app.show_help_panel",
                             "app.screenshot", "quit"]

            await pilot.exit(None)

    asyncio.run(drive())


def test_sidebar_escape_returns_focus_to_main_table() -> None:
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one("#side_menu_options").focus()
            await pilot.pause()
            assert app.focused is not None
            assert app.focused.id == "side_menu_options"

            await pilot.press("escape")
            await pilot.pause()

            assert app.focused is not None
            assert app.focused.id == "main_table"

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


def test_esc_cancels_pending_quit_without_clearing_search(monkeypatch) -> None:
    """Issue #1 regression: Esc during the quit-hint window must cancel only
    the pending quit — the search-clearing tail of action_clear_search (which
    would also hit query_one on this unmounted app) must not run."""
    from kutop.render.app import TopApp

    notices: list[str] = []
    exits: list[bool] = []
    monkeypatch.setattr(TopApp, "notify",
                        lambda self, message, **kwargs: notices.append(message))
    monkeypatch.setattr(TopApp, "exit", lambda self: exits.append(True))

    app = TopApp(
        ["default"],
        config=Config(theme="textual-dark"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    app.action_quit_hint()  # arm the hint
    app.action_clear_search()  # Esc: consumes the pending quit only
    assert "quit cancelled" in notices
    assert exits == []

    app.action_quit_hint()  # re-arms instead of quitting
    assert exits == []
    assert notices.count("Press q again to quit") == 2


def test_quit_hint_timeout_lapse_rearms(monkeypatch) -> None:
    """Issue #1 regression: q after the 4s window expired must re-arm the
    hint, never quit."""
    from time import monotonic

    from kutop.render.app import TopApp

    notices: list[str] = []
    exits: list[bool] = []
    monkeypatch.setattr(TopApp, "notify",
                        lambda self, message, **kwargs: notices.append(message))
    monkeypatch.setattr(TopApp, "exit", lambda self: exits.append(True))

    app = TopApp(
        ["default"],
        config=Config(theme="textual-dark"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    app.action_quit_hint()
    app._quit_hint_deadline = monotonic() - 0.1  # window lapsed
    app.action_quit_hint()
    assert exits == []
    assert notices.count("Press q again to quit") == 2


def test_enter_confirms_only_a_pending_quit(monkeypatch) -> None:
    """Issue #1 regression: the priority Enter binding is gated by
    check_action, so it only exists while the quit hint is pending and falls
    through to focused widgets otherwise."""
    from kutop.render.app import TopApp

    enter = next(b for b in TopApp.BINDINGS
                 if isinstance(b, Binding) and b.action == "confirm_quit")
    assert enter.key == "enter"
    assert enter.priority is True
    assert enter.show is False

    exits: list[bool] = []
    monkeypatch.setattr(TopApp, "notify", lambda self, message, **kwargs: None)
    monkeypatch.setattr(TopApp, "exit", lambda self: exits.append(True))

    app = TopApp(
        ["default"],
        config=Config(theme="textual-dark"),
        discover_namespaces=False,
        auto_refresh=False,
    )

    # idle: the binding is inactive, Enter reaches the focused widget
    assert app.check_action("confirm_quit", ()) is False

    app.action_quit_hint()
    assert app.check_action("confirm_quit", ()) is True
    app.action_confirm_quit()
    assert exits == [True]
    # confirming consumed the window: Enter is inert again
    assert app.check_action("confirm_quit", ()) is False


def test_pending_quit_never_pierces_modals_or_search(monkeypatch) -> None:
    """Review regression: with a quit hint pending, Enter must act on a pushed
    modal or the search input — never fall through the priority app binding
    and quit. Opening either surface abandons the pending quit outright."""
    from time import monotonic

    from textual.widgets import Button

    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    quits: list[bool] = []
    monkeypatch.setattr(TopApp, "action_quit", lambda self: quits.append(True))

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.refresh_snapshot = lambda: None  # type: ignore[assignment]
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()

            # q then x: the modal abandons the pending quit and owns Enter
            app.action_quit_hint()
            assert monotonic() <= app._quit_hint_deadline
            app.push_screen(ConfirmModal("Delete pod?", "pod: demo", "Delete"))
            await pilot.pause()
            assert app._quit_hint_deadline == 0.0
            assert app.check_action("confirm_quit", ()) is False
            # the gate alone must hold even with a live window: a modal on
            # the screen stack disables the confirm regardless of deadline
            app._quit_hint_deadline = monotonic() + 4
            assert app.check_action("confirm_quit", ()) is False
            app._quit_hint_deadline = 0.0
            app.screen.query_one("#confirm_yes", Button).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert quits == []
            assert len(app.screen_stack) == 1  # Enter pressed the modal button

            # q then /: starting a search abandons the pending quit; Enter
            # submits the filter and refocuses the table without quitting
            app.action_quit_hint()
            app.action_search()
            await pilot.pause()
            assert app._quit_hint_deadline == 0.0
            await pilot.press("enter")
            await pilot.pause()
            assert quits == []

            await pilot.exit(None)

    asyncio.run(drive())
    assert quits == []


def test_pending_quit_never_pierces_sidebar_menu(monkeypatch) -> None:
    """Reaching for the sidebar menu abandons a pending quit; Enter inside the
    sidebar (focus is within the sidebar ancestor chain) must never fall through
    the priority app binding and quit — the Options modal should open instead."""
    from time import monotonic

    from kutop.render.app import TopApp
    from kutop.render.widgets import OptionsModal

    quits: list[bool] = []
    monkeypatch.setattr(TopApp, "action_quit", lambda self: quits.append(True))

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.refresh_snapshot = lambda: None  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # (a) arm the quit hint, then open the menu (sidebar visible by default)
            app.action_quit_hint()
            app.action_open_menu()
            await pilot.pause()
            # reaching for the menu abandons the quit window
            assert app._quit_hint_deadline == 0.0
            # focus should land on the Options button in the sidebar
            assert app.focused is not None
            assert app.focused.id == "side_menu_options"

            # (b) re-arm while focus is inside the sidebar — check_action must
            # gate out the binding even with a live deadline
            app.action_quit_hint()
            app._quit_hint_deadline = monotonic() + 4  # ensure alive
            assert app.check_action("confirm_quit", ()) is False

            # pressing Enter with sidebar focus opens the Options modal (not quit)
            await pilot.press("enter")
            await pilot.pause()
            assert quits == []
            assert len(app.screen_stack) > 1
            assert isinstance(app.screen, OptionsModal)
            # close the modal
            await pilot.press("escape")
            await pilot.pause()

            # (c) verify no quit fired throughout
            assert quits == []

    asyncio.run(drive())
    assert quits == []


def test_esc_in_sidebar_cancels_pending_quit(monkeypatch) -> None:
    """Esc while focus is inside the sidebar and a quit is pending must cancel
    the quit (not the sidebar itself) and move focus back to #main_table."""
    from kutop.render.app import TopApp

    quits: list[bool] = []
    monkeypatch.setattr(TopApp, "action_quit", lambda self: quits.append(True))

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(theme="textual-dark"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.refresh_snapshot = lambda: None  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            notices: list[str] = []
            monkeypatch.setattr(
                app, "notify",
                lambda message, **kwargs: notices.append(str(message)),
            )

            # focus the Options button inside the sidebar
            app.query_one("#side_menu_options").focus()
            await pilot.pause()
            assert app.focused is not None
            assert app.focused.id == "side_menu_options"

            # arm the quit hint
            app.action_quit_hint()
            assert app._quit_hint_deadline > 0.0

            # Esc inside the sidebar runs action_focus_main (sidebar binding),
            # which calls _cancel_pending_quit(announce=True) and moves focus
            await pilot.press("escape")
            await pilot.pause()

            # deadline cleared
            assert app._quit_hint_deadline == 0.0
            # a 'quit cancelled' notification was issued
            assert any("quit cancelled" in msg for msg in notices)
            # focus returned to the main table
            assert app.focused is not None
            assert app.focused.id == "main_table"

            # pressing Enter now must not quit
            await pilot.press("enter")
            await pilot.pause()
            assert quits == []

    asyncio.run(drive())
    assert quits == []


def test_options_modal_theme_preview_enter_persists(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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


def test_options_modal_escape_reverts_edits_and_persists_snapshot(monkeypatch) -> None:
    from textual.widgets import Checkbox, Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(panel_backgrounds=True, summary_style="tiles"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            before = app.cfg.to_dict()
            saved.clear()

            checkbox = app.screen.query_one("#opt_panel_backgrounds", Checkbox)
            checkbox.value = False
            await pilot.pause()
            style = app.screen.query_one("#opt_summary_style", Select)
            style.value = "compact"
            await pilot.pause()
            # live preview applied + persisted while the modal is open
            assert app.cfg.panel_backgrounds is False
            assert app.cfg.summary_style == "compact"
            assert saved

            await pilot.press("escape")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert app.cfg.panel_backgrounds is True
            assert app.cfg.summary_style == "tiles"
            assert app.cfg.to_dict() == before

            await pilot.exit(None)

    asyncio.run(drive())

    # the restored snapshot was persisted, not just re-rendered
    assert saved[-1]["view"]["panel_backgrounds"] is True
    assert saved[-1]["view"]["summary_style"] == "tiles"


def test_options_modal_cancel_button_reverts_edits(monkeypatch) -> None:
    from textual.widgets import Checkbox, Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(panel_backgrounds=True, summary_style="tiles"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            before = app.cfg.to_dict()
            saved.clear()

            checkbox = app.screen.query_one("#opt_panel_backgrounds", Checkbox)
            checkbox.value = False
            await pilot.pause()
            style = app.screen.query_one("#opt_summary_style", Select)
            style.value = "compact"
            await pilot.pause()
            assert app.cfg.panel_backgrounds is False

            await pilot.click("#opt_cancel")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert app.cfg.panel_backgrounds is True
            assert app.cfg.summary_style == "tiles"
            assert app.cfg.to_dict() == before

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["view"]["panel_backgrounds"] is True
    assert saved[-1]["view"]["summary_style"] == "tiles"


def test_options_modal_close_keeps_edits(monkeypatch) -> None:
    from textual.widgets import Checkbox

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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

            await pilot.click("#opt_close")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert app.cfg.panel_backgrounds is False

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["view"]["panel_backgrounds"] is False


def test_options_modal_cancel_reverts_previewed_and_committed_theme(monkeypatch) -> None:
    from textual.widgets import Button, Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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

            # previewed-but-uncommitted theme: Cancel restores it, no persist
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

            app.screen.query_one("#opt_cancel", Button).press()
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert app.theme == "textual-dark"
            assert app.cfg.theme == "textual-dark"
            assert saved == []

            # committed theme: Esc re-adopts the snapshot and persists it
            app.action_open_options()
            await pilot.pause()
            select = app.screen.query_one("#opt_theme", Select)
            select.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.cfg.theme == "textual-light"
            saved.clear()

            await pilot.press("escape")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert app.theme == "textual-dark"
            assert app.cfg.theme == "textual-dark"
            assert saved[-1]["view"]["theme"] == "textual-dark"

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


def test_options_modal_add_health_probe_rewires_fetcher_and_persists(monkeypatch) -> None:
    from textual.widgets import Button, Input

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(health_probes=[]),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.fetcher.health_probes == []
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            app.screen.query_one("#opt_hp_name", Input).value = "node-rpc"
            app.screen.query_one("#opt_hp_url", Input).value = "/api/v1/health"
            app.screen.query_one("#opt_hp_field_label", Input).value = "block"
            app.screen.query_one("#opt_hp_field_regex", Input).value = r"height=(\d+)"
            await pilot.pause()

            app.screen.query_one("#opt_hp_add", Button).press()
            await pilot.pause()

            assert app.cfg.health_probes == [
                {"name": "node-rpc", "url": "/api/v1/health",
                 "fields": {"block": r"height=(\d+)"}}
            ]
            # the app re-wired the fetcher so the next refresh scrapes it
            assert [hp.name for hp in app.fetcher.health_probes] == ["node-rpc"]
            assert app.fetcher.health_probes[0].url == "/api/v1/health"
            assert app.fetcher.health_probes[0].fields == {"block": r"height=(\d+)"}

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["probes"]["health_probes"] == [
        {"name": "node-rpc", "url": "/api/v1/health",
         "fields": {"block": r"height=(\d+)"}}
    ]


def test_options_modal_add_health_probe_rejects_bad_url(monkeypatch) -> None:
    from textual.widgets import Button, Input, Label

    from kutop.render.app import TopApp

    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(health_probes=[]),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()

            app.screen.query_one("#opt_hp_name", Input).value = "bad"
            app.screen.query_one("#opt_hp_url", Input).value = "ftp://nope"
            await pilot.pause()
            app.screen.query_one("#opt_hp_add", Button).press()
            await pilot.pause()

            # rejected inline, nothing added, no crash
            assert app.cfg.health_probes == []
            notice = app.screen.query_one("#opt_hp_notice", Label)
            assert "http" in str(notice.render())

            await pilot.exit(None)

    asyncio.run(drive())


def test_options_modal_remove_health_probe_disables_plugin(monkeypatch) -> None:
    from textual.widgets import Button, OptionList

    from kutop.plugins.health import HealthPlugin
    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(
                health_probes=[{"name": "svc", "url": "/api/health", "fields": {}}]
            ),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert HealthPlugin().is_enabled(app.cfg) is True
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            ol = app.screen.query_one("#opt_health_probes", OptionList)
            ol.highlighted = 0
            await pilot.pause()
            app.screen.query_one("#opt_hp_remove", Button).press()
            await pilot.pause()

            assert app.cfg.health_probes == []
            assert HealthPlugin().is_enabled(app.cfg) is False
            assert app.fetcher.health_probes == []

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["probes"]["health_probes"] == []


def test_options_modal_cancel_reverts_health_probe_add(monkeypatch) -> None:
    from textual.widgets import Button, Input

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(health_probes=[]),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_open_options()
            await pilot.pause()
            saved.clear()

            app.screen.query_one("#opt_hp_url", Input).value = "/api/health"
            await pilot.pause()
            app.screen.query_one("#opt_hp_add", Button).press()
            await pilot.pause()
            assert len(app.cfg.health_probes) == 1

            # Esc/Cancel re-adopts the opening snapshot (_orig_cfg) -> no probes
            await pilot.click("#opt_cancel")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert app.cfg.health_probes == []
            assert app.fetcher.health_probes == []

            await pilot.exit(None)

    asyncio.run(drive())

    assert saved[-1]["probes"]["health_probes"] == []


def test_options_modal_added_probe_under_active_profile_not_persisted(
        tmp_path: Path, monkeypatch) -> None:
    import kutop.config as kconfig
    from textual.widgets import Button, Input

    from kutop.render.app import TopApp

    # a profile that itself supplies a health probe -> probes are profile-owned
    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    (tmp_path / "bundle.yaml").write_text(
        """
name: bundle
namespaces: [team-x]
health_probes:
  - name: from-profile
    url: /api/profile-health
    fields: {}
""".strip(),
        encoding="utf-8",
    )

    # Capture the persisted VIEW (after profile-owned stripping) exactly as the
    # real save_config would write it, by routing through _config_for_persist.
    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(
            kconfig._config_for_persist(cfg, kw.get("profile")).to_dict()
        ) or "/tmp/kutop-config.yaml",
    )

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.refresh_snapshot = lambda: None  # type: ignore[assignment]
            app.set_profile("bundle")
            await pilot.pause()
            assert app.profile.name == "bundle"
            assert app.cfg.health_probes == [
                {"name": "from-profile", "url": "/api/profile-health", "fields": {}}
            ]

            app.action_open_options()
            await pilot.pause()
            saved.clear()

            # add a second probe through the editor
            app.screen.query_one("#opt_hp_name", Input).value = "added-live"
            app.screen.query_one("#opt_hp_url", Input).value = "/api/added"
            await pilot.pause()
            app.screen.query_one("#opt_hp_add", Button).press()
            await pilot.pause()

            assert [p["name"] for p in app.cfg.health_probes] == [
                "from-profile", "added-live",
            ]

            await pilot.exit(None)

    asyncio.run(drive())

    # profile-owned stripping holds: the persisted view carries NO health_probes
    # (the profile re-supplies them on the next load; persisting them would leak
    # one context's profile probes into the shared config file).
    assert saved[-1]["probes"]["health_probes"] == []


def test_options_modal_context_input_persists(monkeypatch) -> None:
    from textual.widgets import Select

    from kutop.render.app import TopApp

    saved: list[dict] = []
    monkeypatch.setattr(
        "kutop.render.app.save_config",
        lambda cfg, path=None, **kw: saved.append(cfg.to_dict()) or "/tmp/kutop-config.yaml",
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
            # Context discovery is now async (off the UI thread, so opening
            # Options never blocks on kubectl). At mount the discovery worker
            # calls _context_options() to warm _discovered_contexts; drive that
            # same synchronous, mocked path deterministically here before the
            # modal reads the warmed cache.
            app._context_options()
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
    # the fallback is not silent: a load warning records the unknown theme
    assert any("does-not-exist" in w for w in app.cfg.load_warnings)


def test_load_warnings_surface_as_toasts_on_mount(monkeypatch) -> None:
    from kutop.render.app import TopApp

    seen: list[tuple[str, str]] = []

    async def drive() -> None:
        cfg = Config(theme="does-not-exist")
        cfg.load_warnings.append("config.yaml could not be parsed (boom); using defaults")
        app = TopApp(
            ["default"],
            config=cfg,
            discover_namespaces=False,
            auto_refresh=False,
        )
        monkeypatch.setattr(
            app, "notify",
            lambda message, severity="information", **kw: seen.append((str(message), severity)),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.exit(None)

    asyncio.run(drive())

    warnings = [msg for msg, severity in seen if severity == "warning"]
    assert any("could not be parsed" in msg for msg in warnings)
    assert any("does-not-exist" in msg for msg in warnings)


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


def test_options_cancel_with_profile_preserves_user_fields(tmp_path: Path) -> None:
    """Options Cancel under an active profile with the REAL save path (no
    save_config monkeypatch): namespaces/context/timezone survive because
    _adopt_config now passes profile=self.profile to save_config, so the
    profile's own section never strips user-saved cluster fields.

    Steps:
    - build TopApp with a Profile that owns thresholds only and a Config
      carrying namespaces=['team-a','team-b'], context='user-ctx', timezone='Europe/Berlin'
    - config_path redirected to tmp_path so we write a real file
    - open Options, toggle #opt_p_pvc (Panels tab), then Cancel (Esc)
    - load_config from the written file: namespaces/context/timezone must
      SURVIVE (profile= persist fix) and show_pvc must be back to pre-open value
    """
    from textual.widgets import Checkbox

    from kutop.render.app import TopApp
    from kutop.render.widgets import OptionsModal

    cfg_file = tmp_path / "config.yaml"

    # Profile owns only thresholds — no namespaces/context/timezone
    profile = Profile(
        name="test-profile",
        cpu_warn=60, cpu_crit=85,
        mem_warn=70, mem_crit=88,
    )

    # User config carries the cluster-level fields the profile doesn't own
    user_cfg = Config(
        namespaces=["team-a", "team-b"],
        context="user-ctx",
        timezone="Europe/Berlin",
        profile_name="test-profile",
        show_pvc=False,   # pre-open value we'll try to change then revert
    )

    async def drive() -> None:
        app = TopApp(
            ["team-a", "team-b"],
            profile=profile,
            config=user_cfg,
            context="user-ctx",
            config_path=str(cfg_file),
            discover_namespaces=False,
            auto_refresh=False,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # Force an initial persist so the file exists before the modal
            app.export_config()
            await pilot.pause()

            pre_open_pvc = app.cfg.show_pvc  # False

            # Open Options modal
            app.action_open_options()
            await pilot.pause()

            assert isinstance(app.screen, OptionsModal)
            pvc_cb = app.screen.query_one("#opt_p_pvc", Checkbox)
            pvc_cb.value = True
            await pilot.pause()
            # live preview: field changed in running app
            assert app.cfg.show_pvc is True

            # Cancel (Esc) — reverts the edit and re-persists the snapshot
            await pilot.press("escape")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            # reverted in-memory
            assert app.cfg.show_pvc is pre_open_pvc

            await pilot.exit(None)

    asyncio.run(drive())

    # Load from the real written file and verify all three user-owned fields survived
    saved_cfg = load_config(user_path=str(cfg_file))

    assert saved_cfg.namespaces == ["team-a", "team-b"], (
        f"namespaces lost after Cancel: {saved_cfg.namespaces}"
    )
    assert saved_cfg.context == "user-ctx", (
        f"context lost after Cancel: {saved_cfg.context!r}"
    )
    assert saved_cfg.timezone == "Europe/Berlin", (
        f"timezone lost after Cancel: {saved_cfg.timezone!r}"
    )
    # the cancelled edit is NOT in the file
    assert saved_cfg.show_pvc is False, (
        f"show_pvc should be False (pre-open) but got {saved_cfg.show_pvc}"
    )


# ── beginner UX polish (workstream D) ──────────────────────────────────────────


class _GoodFetcher:
    """Duck-typed fetcher returning one healthy synthetic frame (no kubectl)."""

    def __init__(self, namespaces=None) -> None:
        self.namespaces = list(namespaces or ["default"])

    def fetch_core(self):
        from kutop.model import Node, Pod, Snapshot

        snap = Snapshot()
        snap.nodes = [Node(name="node-a", cpu_mcpu=1, cpu_cap_mcpu=10, ready=True)]
        snap.pods = [
            Pod(name="pod-a", namespace="default", node="node-a",
                phase="Running", ready="1/1")
        ]
        return snap

    def enrich_snapshot(self, snap, *, heavy=True):
        return snap

    def fetch(self):
        return self.enrich_snapshot(self.fetch_core())

    def cancel(self) -> None:
        pass


def test_first_run_hint_shows_once_on_generic_first_load(monkeypatch) -> None:
    """The orientation toast fires exactly once: on the first successful load of
    an out-of-the-box generic config, and never on a later snapshot."""
    from kutop.render.app import TopApp

    notices: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        TopApp, "notify",
        lambda self, message, **kwargs: notices.append((message, kwargs)),
    )

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.fetcher = _GoodFetcher(["default"])  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices.clear()  # ignore any mount-time toasts

            app.refresh_snapshot()
            for _ in range(100):
                await pilot.pause(0.02)
                if app._loaded and not app._fetching:
                    break
            await pilot.pause()

            assert app._loaded is True
            hints = [m for m, _ in notices if "for sidebar" in m]
            assert len(hints) == 1
            msg = hints[0]
            assert "b for sidebar" in msg
            assert "o for options" in msg
            assert "/ to search" in msg
            assert app._shown_first_run_hint is True

            # a second snapshot must NOT re-show the hint
            notices.clear()
            from kutop.model import Snapshot
            app._apply_snapshot(Snapshot())
            await pilot.pause()
            assert not any("for sidebar" in m for m, _ in notices)

            await pilot.exit(None)

    asyncio.run(drive())


def test_first_run_hint_suppressed_for_customized_config(monkeypatch) -> None:
    """A non-default scope (here: a profile-named, multi-namespace config) is a
    returning/power user — the orientation toast must stay silent."""
    from kutop.render.app import TopApp

    notices: list[str] = []
    monkeypatch.setattr(
        TopApp, "notify",
        lambda self, message, **kwargs: notices.append(message),
    )

    async def drive() -> None:
        app = TopApp(
            ["team-a", "team-b"],
            config=Config(namespaces=["team-a", "team-b"], profile_name="acme"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.fetcher = _GoodFetcher(["team-a", "team-b"])  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices.clear()

            app.refresh_snapshot()
            for _ in range(100):
                await pilot.pause(0.02)
                if app._loaded and not app._fetching:
                    break
            await pilot.pause()

            assert app._loaded is True
            assert not any("for sidebar" in m for m in notices)
            # the once-per-session latch still flips, so a later default scope
            # cannot belatedly trigger it
            assert app._shown_first_run_hint is True

            await pilot.exit(None)

    asyncio.run(drive())


def test_empty_state_message_names_filter_term_and_namespaces() -> None:
    """The main-table empty-state row is actionable: a live search names the term
    and how to clear it; a truly empty scope names namespaces + active filters."""
    from textual.widgets import DataTable

    from kutop.model import Node, Snapshot
    from kutop.render.app import TopApp

    async def drive() -> None:
        app = TopApp(
            ["team-a", "team-b"],
            config=Config(namespaces=["team-a", "team-b"]),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.fetcher = _GoodFetcher(["team-a", "team-b"])  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # a snapshot with a node but no pods -> truly-empty unfiltered scope
            snap = Snapshot()
            snap.nodes = [Node(name="node-a", cpu_mcpu=1, cpu_cap_mcpu=10, ready=True)]
            app._apply_snapshot(snap)
            await pilot.pause()

            mt = app.query_one("#main_table", DataTable)
            rows = [str(mt.get_row_at(i)[0]) for i in range(mt.row_count)]
            empty = next(r for r in rows if "no pods" in r)
            assert "team-a" in empty and "team-b" in empty
            assert "hide_completed on" in empty
            assert "b to change namespaces" in empty

            # now drive a live search that matches nothing -> filter-specific text
            app._search_term = "zzz-no-match"
            app._render_main_table()
            await pilot.pause()
            rows = [str(mt.get_row_at(i)[0]) for i in range(mt.row_count)]
            filtered = next(r for r in rows if "no pods" in r)
            assert "zzz-no-match" in filtered
            assert "esc to clear" in filtered

            await pilot.exit(None)

    asyncio.run(drive())


def test_unreachable_guidance_first_row_names_context() -> None:
    """The startup-guidance first row names the resolved kube context so a
    beginner can distinguish 'right cluster, unreachable' from 'wrong context'."""
    from textual.widgets import DataTable

    from kutop.fetch import Fetcher
    from kutop.render.app import TopApp

    class UnreachableFetcher(Fetcher):
        def _run(self, *args: str, timeout: int = 0) -> str:
            raise RuntimeError("Unable to connect to the server: dial tcp: timeout")

    async def drive() -> None:
        app = TopApp(
            ["default"],
            config=Config(context="prod-eu"),
            discover_namespaces=False,
            auto_refresh=False,
        )
        app.fetcher = UnreachableFetcher(["default"])  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            app.refresh_snapshot()
            for _ in range(100):
                await pilot.pause(0.02)
                if not app._fetching:
                    break
            await pilot.pause()

            assert app._loaded is False
            mt = app.query_one("#main_table", DataTable)
            first = str(mt.get_row_at(0)[0])
            assert "cluster unreachable" in first
            assert "context: prod-eu" in first

            await pilot.exit(None)

    asyncio.run(drive())
