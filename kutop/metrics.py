"""Metrics Server preflight and explicit bootstrap helpers.

kutop reads CPU/MEM usage through ``kubectl top``. When the Metrics API is not
installed, the dashboard can otherwise look deceptively healthy with zero usage.
This module keeps the cluster-mutating path explicit: detect first, ask the user
in an interactive terminal, then run the official manifest only on ``y``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, TextIO

from .fetch import current_context_name


METRICS_SERVER_COMPONENTS_URL = (
    "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
)
METRICS_SERVER_DOCS_URL = "https://kubernetes-sigs.github.io/metrics-server/"
METRICS_SERVER_HELM_REPO = "https://kubernetes-sigs.github.io/metrics-server/"


@dataclass(frozen=True)
class MetricsPreflight:
    """Result of checking whether ``kubectl top`` can return live usage."""

    status: str
    message: str = ""

    @property
    def available(self) -> bool:
        return self.status == "available"


def _kubectl_cmd(context: Optional[str], *args: str) -> list[str]:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd.extend(args)
    return cmd


def _run_kubectl(context: Optional[str], *args: str, timeout: int = 6) -> subprocess.CompletedProcess:
    cmd = _kubectl_cmd(context, *args)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "kubectl not found on PATH")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{shlex.join(cmd)} timed out")


def _proc_text(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()


def _looks_missing_metrics_api(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in (
            "metrics api not available",
            "the server could not find the requested resource",
            "no matches for kind",
            "server doesn't have a resource type",
            "notfound",
        )
    )


def check_metrics_preflight(context: Optional[str] = None, timeout: int = 6) -> MetricsPreflight:
    """Verify both ``kubectl top`` and the metrics.k8s.io discovery endpoint.

    ``kubectl top`` is the functional check kutop actually depends on. The raw
    discovery call is only used to distinguish a missing Metrics API from other
    failures such as RBAC, TLS, transient API-server errors, or a broken install.
    """

    top = _run_kubectl(context, "top", "nodes", "--no-headers", timeout=timeout)
    if top.returncode == 0:
        return MetricsPreflight("available")

    api = _run_kubectl(
        context,
        "get",
        "--raw",
        "/apis/metrics.k8s.io/v1beta1",
        timeout=timeout,
    )
    combined = "\n".join(part for part in (_proc_text(top), _proc_text(api)) if part)
    if top.returncode == 127:
        return MetricsPreflight("kubectl-missing", combined)
    if _looks_missing_metrics_api(combined):
        return MetricsPreflight("missing", combined)
    return MetricsPreflight("unavailable", combined)


def _install_review_text() -> str:
    return "\n".join(
        [
            "[kutop] Metrics Server install options:",
            f"[kutop]   components: kubectl apply -f {METRICS_SERVER_COMPONENTS_URL}",
            f"[kutop]   helm repo : helm repo add metrics-server {METRICS_SERVER_HELM_REPO}",
            "[kutop]   helm      : helm upgrade --install metrics-server "
            "metrics-server/metrics-server -n kube-system --create-namespace",
            f"[kutop]   docs      : {METRICS_SERVER_DOCS_URL}",
        ]
    )


def _is_interactive(input_stream: TextIO, output_stream: TextIO) -> bool:
    return bool(
        getattr(input_stream, "isatty", lambda: False)()
        and getattr(output_stream, "isatty", lambda: False)()
    )


def _answer_is_yes(answer: str) -> bool:
    # Exact y/yes only: this gates a kubectl apply against the cluster, so a
    # mistyped "y..." with second thoughts in it must not count as consent.
    return answer.strip().lower() in ("y", "yes")


def maybe_bootstrap_metrics_server(
    *,
    context: Optional[str] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    interactive: Optional[bool] = None,
    timeout: int = 6,
) -> MetricsPreflight:
    """Run the startup metrics check and optionally install Metrics Server.

    The only cluster-mutating branch is the explicit ``y`` answer to the prompt.
    ``N`` or non-interactive execution leaves the cluster untouched and prints
    both the manifest and Helm paths so the operator can choose deliberately.
    """

    if os.environ.get("KUTOP_NO_METRICS_BOOTSTRAP"):
        return MetricsPreflight("skipped", "KUTOP_NO_METRICS_BOOTSTRAP is set")

    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stderr
    # The two kubectl probes below can block up to ~2x the timeout before the
    # fullscreen TUI appears; say so up front instead of looking hung.
    print(
        f"[kutop] checking metrics-server (up to ~{2 * timeout}s; "
        "skip with --no-metrics-bootstrap)…",
        file=output_stream,
    )
    output_stream.flush()
    result = check_metrics_preflight(context=context, timeout=timeout)
    if result.available:
        return result

    ctx = context or "current context"
    if result.status == "kubectl-missing":
        print("[kutop] kubectl was not found on PATH; live dashboard mode requires kubectl.", file=output_stream)
        return result

    if result.status == "unavailable":
        print(
            f"[kutop] Metrics API check failed for {ctx}; leaving the cluster unchanged.",
            file=output_stream,
        )
        if result.message:
            print(f"[kutop] kubectl detail: {result.message.splitlines()[0]}", file=output_stream)
        print(_install_review_text(), file=output_stream)
        return result

    print(
        f"[kutop] metrics.k8s.io is not available for {ctx}; CPU/MEM usage needs Metrics Server.",
        file=output_stream,
    )
    should_prompt = _is_interactive(input_stream, output_stream) if interactive is None else interactive
    if should_prompt:
        # Name the context the apply would actually mutate so a reflexive 'y'
        # cannot silently target the wrong cluster. Default is No.
        prompt_ctx = current_context_name(context)
        target = f"context '{prompt_ctx}'" if prompt_ctx else "the current context"
        output_stream.write(
            f"[kutop] Install Metrics Server into {target} via the official "
            "components manifest now? [y/N] "
        )
        output_stream.flush()
        if _answer_is_yes(input_stream.readline()):
            cmd = _kubectl_cmd(context, "apply", "-f", METRICS_SERVER_COMPONENTS_URL)
            print(f"[kutop] running: {shlex.join(cmd)}", file=output_stream)
            proc = subprocess.run(cmd, text=True, check=False)
            if proc.returncode == 0:
                print(
                    "[kutop] Metrics Server manifest applied. It may take a minute before kubectl top returns data.",
                    file=output_stream,
                )
            else:
                print("[kutop] Metrics Server install failed; review manual options below.", file=output_stream)
                print(_install_review_text(), file=output_stream)
            return MetricsPreflight("installed" if proc.returncode == 0 else "install-failed", result.message)

    print("[kutop] Metrics Server was not installed automatically.", file=output_stream)
    print(_install_review_text(), file=output_stream)
    return result
