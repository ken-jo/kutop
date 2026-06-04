from __future__ import annotations

import io
import subprocess

from kutop.metrics import (
    METRICS_SERVER_COMPONENTS_URL,
    _answer_is_yes,
    check_metrics_preflight,
    maybe_bootstrap_metrics_server,
)


def _cp(cmd: list[str], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, code, out, err)


def test_preflight_reports_available_when_kubectl_top_works(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _cp(list(cmd), 0, out="node-a 10m 1% 20Mi 1%\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = check_metrics_preflight(context="dev")

    assert result.status == "available"
    assert calls == [["kubectl", "--context", "dev", "top", "nodes", "--no-headers"]]


def test_prompt_answer_accepts_case_insensitive_yes_prefix() -> None:
    for answer in ("y", "Y", "yes", "YES", "Yes", " yep"):
        assert _answer_is_yes(answer)

    for answer in ("n", "N", "no", "NO", "No", "", " maybe"):
        assert not _answer_is_yes(answer)


def test_missing_metrics_api_declined_logs_install_options(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "top" in cmd:
            return _cp(list(cmd), 1, err="error: Metrics API not available")
        return _cp(
            list(cmd),
            1,
            err="Error from server (NotFound): the server could not find the requested resource",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = io.StringIO()

    result = maybe_bootstrap_metrics_server(
        context="dev",
        input_stream=io.StringIO("NO\n"),
        output_stream=output,
        interactive=True,
    )

    text = output.getvalue()
    assert result.status == "missing"
    assert "metrics.k8s.io is not available for dev" in text
    assert "[y/n]" in text
    assert "Metrics Server was not installed automatically" in text
    assert f"kubectl apply -f {METRICS_SERVER_COMPONENTS_URL}" in text
    assert "helm upgrade --install metrics-server metrics-server/metrics-server" in text
    assert not any("apply" in cmd for cmd in calls)


def test_missing_metrics_api_accept_runs_components_manifest(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "top" in cmd:
            return _cp(list(cmd), 1, err="error: Metrics API not available")
        if "get" in cmd:
            return _cp(
                list(cmd),
                1,
                err="Error from server (NotFound): the server could not find the requested resource",
            )
        return _cp(list(cmd), 0, out="metrics-server configured\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = io.StringIO()

    result = maybe_bootstrap_metrics_server(
        context="dev",
        input_stream=io.StringIO("Yes\n"),
        output_stream=output,
        interactive=True,
    )

    assert result.status == "installed"
    assert calls[-1] == [
        "kubectl",
        "--context",
        "dev",
        "apply",
        "-f",
        METRICS_SERVER_COMPONENTS_URL,
    ]
    assert "Metrics Server manifest applied" in output.getvalue()


def test_top_failure_with_existing_metrics_api_does_not_apply(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "top" in cmd:
            return _cp(list(cmd), 1, err="Error from server (Forbidden): nodes.metrics.k8s.io is forbidden")
        return _cp(list(cmd), 0, out='{"kind":"APIResourceList"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = io.StringIO()

    result = maybe_bootstrap_metrics_server(
        input_stream=io.StringIO("y\n"),
        output_stream=output,
        interactive=True,
    )

    assert result.status == "unavailable"
    assert "leaving the cluster unchanged" in output.getvalue()
    assert not any("apply" in cmd for cmd in calls)
