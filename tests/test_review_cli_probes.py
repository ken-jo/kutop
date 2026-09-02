"""Regression tests for the 2026-09 review pass on probes/CLI/config/metrics.

Each test pins one behaviour that a review found wrong (or unguarded) in
``kutop/probes.py``, ``kutop/snapshot.py``, ``kutop/config.py``, ``kutop/cli.py``,
``kutop/metrics.py``, ``kutop/plugins/`` and ``tools/snapshot.py``.
"""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import kutop.config as kconfig
import kutop.fetch as kfetch
import kutop.metrics as kmetrics
from kutop.config import Config, load_config, load_profile, save_config
from kutop.metrics import _looks_missing_metrics_api, maybe_bootstrap_metrics_server
from kutop.model import Node, Snapshot
from kutop.plugins import iter_plugins, reset_registry
from kutop.plugins.health import HealthPlugin
from kutop.probes import _MAX_BODY_BYTES, _http_get, scrape_probe
from kutop.snapshot import SnapshotResult, _live_snapshot


# ── 1. probes._http_get: scheme allow-list + bounded body ────────────────────


def test_http_get_rejects_non_http_schemes(tmp_path: Path) -> None:
    """A hand-edited profile URL must never be able to read a local file."""
    secret = tmp_path / "secret.txt"
    secret.write_text("kubeconfig-token", encoding="utf-8")

    assert _http_get(secret.as_uri(), 1.0) is None
    assert _http_get("ftp://example.invalid/x", 1.0) is None
    assert _http_get("/api/v1/healthz", 1.0) is None  # no scheme at all


def test_http_get_reads_at_most_the_body_cap(monkeypatch) -> None:
    """The response body is read with an explicit cap, not unbounded."""
    requested: list = []

    class _Resp:
        status = 200

        def read(self, n=None):
            requested.append(n)
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())

    assert _http_get("http://example.invalid/metrics", 1.0) == "ok"
    assert requested == [_MAX_BODY_BYTES + 1]   # one extra byte detects overflow


def test_http_get_rejects_an_oversize_body(monkeypatch) -> None:
    """A body over the cap is reported as no data, never handed over truncated
    (a cut JSON would otherwise read as 'no alerts firing')."""

    class _Resp:
        status = 200

        def read(self, n=None):
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    assert _http_get("http://example.invalid/metrics", 1.0) is None


# ── 2. probes.scrape_probe: patterns screened by regexsafe ───────────────────


def test_scrape_probe_skips_catastrophic_backtracking_pattern() -> None:
    """``(a+)+$`` on a non-matching body would hang the fetch worker forever;
    the field is screened out instead, and the probe still reports reachable."""
    body = "a" * 39 + "b"          # 40 chars, never matches the anchored pattern
    started = time.monotonic()

    result = scrape_probe(
        "api",
        "/api",
        {"boom": r"(a+)+$", "ok": r"(b)$"},
        getter=lambda _url, _timeout: body,
    )

    assert time.monotonic() - started < 2.0
    assert result.ok is True
    assert "boom" not in result.fields
    assert result.fields == {"ok": "b"}


def test_scrape_probe_skips_invalid_pattern_without_raising() -> None:
    result = scrape_probe(
        "api", "/api", {"bad": "("},
        getter=lambda _url, _timeout: "ready=true",
    )

    assert result.ok is True
    assert result.fields == {}


# ── 3. snapshot: a partial live frame beats a synthetic one ──────────────────


def _fake_fetcher_returning(snap: Snapshot):
    class _FakeFetcher:
        def __init__(self, **kwargs) -> None:
            pass

        def fetch(self) -> Snapshot:
            return snap

    return _FakeFetcher


def test_live_snapshot_keeps_partial_frame_that_has_data(monkeypatch) -> None:
    snap = Snapshot()
    snap.nodes = [Node(name="node-a", role="worker", ready=True)]
    snap.error = "top nodes: metrics API not available"
    monkeypatch.setattr(kfetch, "Fetcher", _fake_fetcher_returning(snap))

    live = _live_snapshot(["default"])

    assert live is snap
    assert live.nodes[0].name == "node-a"


def test_live_snapshot_returns_none_when_frame_has_no_data(monkeypatch) -> None:
    empty = Snapshot()
    empty.error = "cluster unreachable"
    monkeypatch.setattr(kfetch, "Fetcher", _fake_fetcher_returning(empty))

    assert _live_snapshot(["default"]) is None


def test_snapshot_result_is_an_exit_code_that_reports_its_source() -> None:
    """Existing callers compare it to 0; new callers can ask what was drawn."""
    live = SnapshotResult(0, synthetic=False, error="partial")
    fallback = SnapshotResult(0, synthetic=True)

    assert live == 0 and fallback == 0
    assert live.synthetic is False and live.error == "partial"
    assert fallback.synthetic is True and fallback.error == ""


def test_cli_snapshot_names_the_synthetic_fallback_and_partial_data(
        monkeypatch, tmp_path: Path, capsys) -> None:
    from kutop import cli

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            pass

    monkeypatch.setattr("kutop.render.app.TopApp", FakeApp)
    out = tmp_path / "frame.svg"

    monkeypatch.setattr(
        "kutop.snapshot.render_snapshot",
        lambda *a, **k: SnapshotResult(0, synthetic=True),
    )
    assert cli.main(["--snapshot", str(out)]) == 0
    captured = capsys.readouterr()
    assert "(synthetic frame: no cluster data)" in captured.out

    monkeypatch.setattr(
        "kutop.snapshot.render_snapshot",
        lambda *a, **k: SnapshotResult(0, synthetic=False, error="get pvc: forbidden"),
    )
    assert cli.main(["--snapshot", str(out)]) == 0
    captured = capsys.readouterr()
    assert "synthetic" not in captured.out
    assert "note: partial data — get pvc: forbidden" in captured.err


# ── 4. config: a null YAML value never becomes the string "None" ─────────────


def test_null_yaml_scalars_load_as_empty_strings(tmp_path: Path) -> None:
    user = tmp_path / "config.yaml"
    user.write_text(
        "view:\n"
        "  timezone:\n"
        "  theme:\n"
        "  sort_key:\n"
        "  summary_style:\n"
        "cluster:\n"
        "  context:\n"
        "filters:\n"
        "  name_filter:\n"
        "profile:\n",
        encoding="utf-8",
    )

    cfg = load_config(user_path=str(user))

    assert cfg.context == ""
    assert cfg.timezone == ""
    assert cfg.name_filter == ""
    assert cfg.theme == "textual-dark"
    assert cfg.sort_key == "priority"
    assert cfg.summary_style == "compact"
    assert cfg.profile_name == "generic"
    assert "None" not in (cfg.context + cfg.timezone + cfg.name_filter)


# ── 5. config.load_profile: defensive coercion, ValueError only ──────────────


def test_load_profile_accepts_null_thresholds_and_sections(tmp_path: Path) -> None:
    prof = tmp_path / "p.yaml"
    prof.write_text(
        "name: p\nthresholds:\nordering:\nhealth_probes:\nnamespaces:\n",
        encoding="utf-8",
    )

    loaded = load_profile(str(prof))

    assert loaded.cpu_warn == 75 and loaded.pvc_crit == 90
    assert loaded.ordering == [] and loaded.health_probes == []
    assert loaded.namespaces == []


def test_load_profile_coerces_scalar_namespaces_to_a_list(tmp_path: Path) -> None:
    prof = tmp_path / "p.yaml"
    prof.write_text("name: p\nnamespaces: prod\n", encoding="utf-8")

    assert load_profile(str(prof)).namespaces == ["prod"]


def test_load_profile_raises_value_error_for_bad_shapes(tmp_path: Path) -> None:
    bad_th = tmp_path / "th.yaml"
    bad_th.write_text("name: p\nthresholds: 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="thresholds must be a mapping"):
        load_profile(str(bad_th))

    bad_val = tmp_path / "val.yaml"
    bad_val.write_text("name: p\nthresholds:\n  cpu_warn: high\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cpu_warn"):
        load_profile(str(bad_val))

    bad_ns = tmp_path / "ns.yaml"
    bad_ns.write_text("name: p\nnamespaces:\n  a: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="namespaces must be a list"):
        load_profile(str(bad_ns))


# ── 6. config.save_config: private by default, preserves an explicit mode ────


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def test_save_config_creates_a_private_file(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"

    save_config(Config(), path=str(target))

    assert _mode(target) == 0o600


def test_save_config_preserves_an_existing_target_mode(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("view: {}\n", encoding="utf-8")
    os.chmod(str(target), 0o644)

    save_config(Config(), path=str(target))

    assert _mode(target) == 0o644


# ── 7. cli ───────────────────────────────────────────────────────────────────


def test_dump_config_never_shells_out_to_kubectl(monkeypatch, tmp_path: Path,
                                                 capsys) -> None:
    """--dump-config is a cluster-free mode; profile recall must not run."""
    from kutop import cli

    user = tmp_path / "config.yaml"
    user.write_text(
        "view:\n"
        "  remember_profile_per_context: true\n"
        "profiles_by_context:\n"
        '  "ctx-a": "someprofile"\n',
        encoding="utf-8",
    )

    def fail_run(cmd, **kwargs):
        raise AssertionError(f"kubectl must not run for --dump-config: {cmd}")

    monkeypatch.setattr(subprocess, "run", fail_run)
    monkeypatch.setattr(
        kfetch, "current_context_name",
        lambda *a, **k: pytest.fail("current_context_name must not be called"),
    )

    assert cli.main(["--dump-config", "--config", str(user)]) == 0
    assert "profile:" in capsys.readouterr().out


def test_empty_snapshot_path_is_a_parser_error(capsys) -> None:
    from kutop import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--snapshot", ""])

    assert exc.value.code == 2
    assert "--snapshot requires a path" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-2", "100001", "abc"])
def test_log_tail_rejects_out_of_range_values(value: str, capsys) -> None:
    from kutop import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--log-tail", value, "--self-test"])

    assert exc.value.code == 2
    assert "--log-tail" in capsys.readouterr().err


@pytest.mark.parametrize("value,expected", [("-1", -1), ("1", 1), ("100000", 100000)])
def test_log_tail_accepts_all_and_the_supported_range(value: str,
                                                      expected: int) -> None:
    from kutop import cli

    args = cli._build_parser().parse_args(["--log-tail", value])

    assert args.log_tail == expected


def test_main_hands_the_app_the_inputs_needed_to_reload_identically(
        monkeypatch, tmp_path: Path) -> None:
    """Hot reload (R) must be able to re-run the exact CLI layering."""
    from kutop import cli

    captured: list = []

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        def run(self) -> None:
            pass

    monkeypatch.setattr(cli.shutil, "which", lambda cmd: "/usr/bin/kubectl")
    monkeypatch.setattr("kutop.render.app.TopApp", FakeApp)

    assert cli.main([
        "team-x",
        "--tz", "Asia/Seoul",
        "--config", str(tmp_path / "config.yaml"),
        "--no-metrics-bootstrap",
    ]) == 0

    reload_overrides = captured[0]["reload_overrides"]
    assert reload_overrides["base_overrides"] == {"cluster": {"namespaces": ["team-x"]}}
    assert reload_overrides["cli_overrides"] == {"view": {"timezone": "Asia/Seoul"}}
    assert reload_overrides["profile_authoritative"] is False


def test_reload_overrides_mark_a_recalled_profile_authoritative(
        monkeypatch, tmp_path: Path) -> None:
    from kutop import cli

    monkeypatch.setattr(kconfig, "_USER_PROFILE_DIR", str(tmp_path))
    (tmp_path / "recalled.yaml").write_text(
        "name: recalled\nnamespaces: [team-y]\n", encoding="utf-8")
    user = tmp_path / "config.yaml"
    user.write_text('profile: "recalled"\n', encoding="utf-8")

    captured: list = []

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        def run(self) -> None:
            pass

    monkeypatch.setattr(cli.shutil, "which", lambda cmd: "/usr/bin/kubectl")
    monkeypatch.setattr("kutop.render.app.TopApp", FakeApp)

    assert cli.main([
        "--config", str(user), "--no-metrics-bootstrap",
    ]) == 0

    assert captured[0]["profile"].name == "recalled"
    assert captured[0]["reload_overrides"]["profile_authoritative"] is True


# ── 8. metrics ───────────────────────────────────────────────────────────────


def test_metrics_bootstrap_env_var_honours_an_explicit_off_value(monkeypatch) -> None:
    """``KUTOP_NO_METRICS_BOOTSTRAP=0`` means 'do not skip'."""
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "node-a 10m 1%\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    for off in ("0", "false", "no", "off", " OFF ", ""):
        monkeypatch.setenv("KUTOP_NO_METRICS_BOOTSTRAP", off)
        calls.clear()
        result = maybe_bootstrap_metrics_server(
            context="dev", input_stream=io.StringIO(""),
            output_stream=io.StringIO(), interactive=False,
        )
        assert result.status == "available", off
        assert calls, off

    monkeypatch.setenv("KUTOP_NO_METRICS_BOOTSTRAP", "1")
    calls.clear()
    assert maybe_bootstrap_metrics_server(
        context="dev", input_stream=io.StringIO(""),
        output_stream=io.StringIO(), interactive=False,
    ).status == "skipped"
    assert calls == []


def test_install_apply_is_bounded_and_a_timeout_is_an_install_failure(
        monkeypatch) -> None:
    seen: list = []

    def fake_run(cmd, **kwargs):
        seen.append((list(cmd), kwargs.get("timeout")))
        if "apply" in cmd:
            raise subprocess.TimeoutExpired(list(cmd), kwargs.get("timeout") or 0)
        if "top" in cmd:
            return subprocess.CompletedProcess(
                list(cmd), 1, "", "error: Metrics API not available")
        return subprocess.CompletedProcess(
            list(cmd), 1, "",
            "Error from server (NotFound): the server could not find the requested resource")

    monkeypatch.delenv("KUTOP_NO_METRICS_BOOTSTRAP", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    output = io.StringIO()

    result = maybe_bootstrap_metrics_server(
        context="dev", input_stream=io.StringIO("y\n"),
        output_stream=output, interactive=True,
    )

    assert result.status == "install-failed"
    assert "Metrics Server install failed" in output.getvalue()
    apply_calls = [(cmd, to) for cmd, to in seen if "apply" in cmd]
    assert apply_calls and apply_calls[0][1] == kmetrics._INSTALL_TIMEOUT_SECS


def test_missing_metrics_api_detection_ignores_unrelated_notfound() -> None:
    # a missing namespace/node is NOT a missing Metrics API
    assert not _looks_missing_metrics_api(
        'namespaces "team-x" not found (reason: notfound)')
    # the API-server's own shape, and anything naming metrics.k8s.io, still count
    assert _looks_missing_metrics_api(
        "Error from server (NotFound): unknown")
    assert _looks_missing_metrics_api(
        "the server could not find metrics.k8s.io: notfound")
    assert _looks_missing_metrics_api("error: Metrics API not available")


# ── 9. plugins ───────────────────────────────────────────────────────────────


def test_health_plugin_surfaces_an_invalid_probe_config() -> None:
    class _Fetcher:
        health_probes = [{"name": "api", "url": "/api", "fields": 5}]  # fields: not a map

    snap = Snapshot()
    HealthPlugin().fetch(_Fetcher(), snap)

    assert len(snap.health) == 1
    assert snap.health[0].ok is False
    assert snap.health[0].error == "invalid probe config"


def test_plugin_registry_init_is_lock_guarded() -> None:
    import threading

    from kutop import plugins as kplugins

    assert isinstance(kplugins._REGISTRY_LOCK, type(threading.Lock()))

    reset_registry()
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(iter_plugins()))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # every thread observes the SAME cached plugin instances
    assert all(r == results[0] for r in results)


# ── 11. tools/snapshot.py default output path ────────────────────────────────


def _load_tools_snapshot():
    path = Path(__file__).resolve().parents[1] / "tools" / "snapshot.py"
    spec = importlib.util.spec_from_file_location("kutop_tools_snapshot", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tools_snapshot_default_output_is_private_and_unpredictable() -> None:
    module = _load_tools_snapshot()

    first, second = module._default_out(), module._default_out()
    try:
        assert first != second
        for path in (first, second):
            assert os.path.basename(path).startswith("kutop-")
            assert path.endswith(".svg")
            assert _mode(path) == 0o600
    finally:
        for path in (first, second):
            try:
                os.unlink(path)
            except OSError:
                pass
