"""Tests for the pod-action features: shell-into-pod and crashloop forensics."""

from __future__ import annotations

import json

from kutop.fetch import Fetcher
from kutop.render.modals import LogViewerModal


class CrashloopFetcher(Fetcher):
    """One multi-container pod whose app container crashloops with exit 137."""

    def _run_safe(self, *args: str, timeout: int = 0) -> str:
        if " ".join(args) == "get pods -n default -o json":
            return json.dumps({"items": [{
                "metadata": {"name": "web-0"},
                "spec": {"containers": [{"name": "app"}, {"name": "sidecar"}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {"name": "app", "ready": False, "restartCount": 7,
                         "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                         "lastState": {"terminated": {"reason": "OOMKilled",
                                                      "exitCode": 137}}},
                        {"name": "sidecar", "ready": True, "restartCount": 0},
                    ],
                },
            }]})
        return ""


def test_parse_pod_captures_containers_and_exit_code() -> None:
    pod = CrashloopFetcher(["default"])._fetch_pods()[0]
    assert pod.container_names == ["app", "sidecar"]
    assert pod.last_exit_code == 137
    # the CURRENT waiting reason outranks the previous termination reason
    # (documented priority in _parse_pod); the exit code still comes from the
    # last termination, which is exactly the crashloop-forensics pairing
    assert pod.last_terminated_reason == "CrashLoopBackOff"
    assert pod.crashloop


def test_log_viewer_command_modes() -> None:
    m = LogViewerModal("web-0", "default", 150, "ctx-a",
                       containers=["app", "sidecar"], status_line="OOMKilled exit=137")
    # live mode targets the first container and follows
    assert m._logs_cmd() == [
        "kubectl", "--context", "ctx-a", "logs", "-n", "default", "web-0",
        "--tail=150", "-c", "app", "-f",
    ]
    # previous mode is static: --previous, no -f
    m._previous = True
    assert m._logs_cmd()[-1] == "--previous"
    assert "-f" not in m._logs_cmd()
    # container cycling wraps around
    m._container_idx += 1
    assert m.container == "sidecar"
    m._container_idx += 1
    assert m.container == "app"
    # header carries the forensic context and the key hints
    head = m._header_text()
    assert "OOMKilled exit=137" in head and "PREVIOUS" in head and "c container" in head


def test_log_viewer_without_container_list_uses_kubectl_default() -> None:
    m = LogViewerModal("solo", "ns1", 50, None)
    cmd = m._logs_cmd()
    assert "-c" not in cmd
    assert cmd[-1] == "-f"
    assert "c container" not in m._header_text()  # no picker hint for one target


def test_shell_cmd_targets_focused_pod() -> None:
    from kutop.model import Pod
    from kutop.render.app import TopApp

    app = TopApp(namespaces=["default"], context="ctx-b",
                 discover_namespaces=False, auto_refresh=False)
    cmd = app._shell_cmd(Pod(name="web-0", namespace="payments"))
    assert cmd[:3] == ["kubectl", "--context", "ctx-b"]
    assert ["exec", "-it", "-n", "payments", "web-0", "--"] == cmd[3:9]
    # bash is preferred, sh is the fallback — via argv, never shell=True
    assert cmd[9:] == ["sh", "-c",
                       "command -v bash >/dev/null 2>&1 && exec bash || exec sh"]
