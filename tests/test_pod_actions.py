"""Tests for the pod-action features: shell-into-pod and crashloop forensics."""

from __future__ import annotations

import asyncio
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


def test_delete_confirm_shows_context_namespace_and_pod() -> None:
    """Issue #4 slice A: the delete confirm must spell out the full target
    identity — cluster context, namespace, and pod name — before executing."""
    from kutop.model import Pod, Snapshot
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        app = TopApp(["payments"], context="ctx-b", allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            snap = Snapshot()
            snap.pods = [Pod(name="web-0", namespace="payments", node="node-a",
                             phase="Running", ready="1/1")]
            app._apply_snapshot(snap)
            await pilot.pause()

            app.action_delete_pod()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            body = app.screen._body
            assert "context: ctx-b" in body
            assert "namespace: payments" in body
            assert "pod: web-0" in body
            await pilot.exit(None)

    asyncio.run(drive())


# ── rollout-restart (X) ───────────────────────────────────────────────────────


def test_restart_cmd_for_each_rollable_owner() -> None:
    from kutop.model import Pod
    from kutop.render.app import TopApp

    app = TopApp(namespaces=["default"], context="ctx-b",
                 discover_namespaces=False, auto_refresh=False)

    # Deployment via raw ReplicaSet owner: the pod-template-hash is stripped
    rs_pod = Pod(name="web-7d4b9c6f9d-abcde", namespace="default",
                 owner_kind="ReplicaSet", owner_name="web-7d4b9c6f9d")
    target, reason = app._rollout_target(rs_pod)
    assert (target, reason) == ("deployment/web", "")
    assert app._restart_cmd(target, "default") == [
        "kubectl", "--context", "ctx-b",
        "rollout", "restart", "deployment/web", "-n", "default",
    ]

    sts_pod = Pod(name="db-0", namespace="data",
                  owner_kind="StatefulSet", owner_name="db")
    target, reason = app._rollout_target(sts_pod)
    assert (target, reason) == ("statefulset/db", "")
    assert app._restart_cmd(target, "data") == [
        "kubectl", "--context", "ctx-b",
        "rollout", "restart", "statefulset/db", "-n", "data",
    ]

    ds_pod = Pod(name="fluentd-x1z2", namespace="logging",
                 owner_kind="DaemonSet", owner_name="fluentd")
    target, reason = app._rollout_target(ds_pod)
    assert (target, reason) == ("daemonset/fluentd", "")
    assert app._restart_cmd(target, "logging") == [
        "kubectl", "--context", "ctx-b",
        "rollout", "restart", "daemonset/fluentd", "-n", "logging",
    ]

    # without an explicit context the --context plumbing disappears (like delete)
    bare = TopApp(namespaces=["default"],
                  discover_namespaces=False, auto_refresh=False)
    assert bare._restart_cmd("deployment/web", "default") == [
        "kubectl", "rollout", "restart", "deployment/web", "-n", "default",
    ]


def test_rollout_target_accepts_fetch_resolved_deployment_owner() -> None:
    """fetch.py already maps a controller ReplicaSet to its Deployment name."""
    from kutop.render.app import TopApp

    class OwnedFetcher(Fetcher):
        def _run_safe(self, *args: str, timeout: int = 0) -> str:
            if " ".join(args) == "get pods -n default -o json":
                return json.dumps({"items": [{
                    "metadata": {
                        "name": "web-7d4b9c6f9d-abcde",
                        "ownerReferences": [{"kind": "ReplicaSet",
                                             "name": "web-7d4b9c6f9d",
                                             "controller": True}],
                    },
                    "spec": {},
                    "status": {"phase": "Running"},
                }]})
            return ""

    pod = OwnedFetcher(["default"])._fetch_pods()[0]
    assert (pod.owner_kind, pod.owner_name) == ("Deployment", "web")
    app = TopApp(namespaces=["default"],
                 discover_namespaces=False, auto_refresh=False)
    assert app._rollout_target(pod) == ("deployment/web", "")


def test_fetch_rs_non_hash_suffix_keeps_replicaset_identity() -> None:
    """A ReplicaSet whose name suffix does NOT look like a pod-template-hash
    (e.g. 'web-canary') must be retained as owner_kind='ReplicaSet' with its
    full name — it must not be mistaken for a Deployment named 'web'."""
    from kutop.render.app import TopApp

    class CanaryFetcher(Fetcher):
        def _run_safe(self, *args: str, timeout: int = 0) -> str:
            if " ".join(args) == "get pods -n default -o json":
                return json.dumps({"items": [{
                    "metadata": {
                        "name": "web-canary-xyzzy",
                        "ownerReferences": [{"kind": "ReplicaSet",
                                             "name": "web-canary",
                                             "controller": True}],
                    },
                    "spec": {},
                    "status": {"phase": "Running"},
                }]})
            return ""

    pod = CanaryFetcher(["default"])._fetch_pods()[0]
    # non-hash suffix: owner stays ReplicaSet, NOT Deployment "web"
    assert pod.owner_kind == "ReplicaSet"
    assert pod.owner_name == "web-canary"

    # and _rollout_target correctly reports it as un-rollable
    app = TopApp(namespaces=["default"],
                 discover_namespaces=False, auto_refresh=False)
    target, reason = app._rollout_target(pod)
    assert target is None
    assert "web-canary" in reason

    # hash-like suffixes still resolve to Deployment (regression guard)
    class HashFetcher(Fetcher):
        def _run_safe(self, *args: str, timeout: int = 0) -> str:
            if " ".join(args) == "get pods -n default -o json":
                return json.dumps({"items": [{
                    "metadata": {
                        "name": "web-7d4b9c6f9d-abcde",
                        "ownerReferences": [{"kind": "ReplicaSet",
                                             "name": "web-7d4b9c6f9d",
                                             "controller": True}],
                    },
                    "spec": {},
                    "status": {"phase": "Running"},
                }]})
            return ""

    hash_pod = HashFetcher(["default"])._fetch_pods()[0]
    assert hash_pod.owner_kind == "Deployment"
    assert hash_pod.owner_name == "web"


def test_rollout_target_rejects_unrollable_pods() -> None:
    from kutop.model import Pod
    from kutop.render.app import TopApp

    app = TopApp(namespaces=["default"],
                 discover_namespaces=False, auto_refresh=False)

    target, reason = app._rollout_target(Pod(name="solo", namespace="default"))
    assert target is None and "no controller" in reason

    target, reason = app._rollout_target(
        Pod(name="migrate-abc", namespace="default",
            owner_kind="Job", owner_name="migrate"))
    assert target is None and "Job" in reason

    # a ReplicaSet name without a '-<hash>' segment cannot name a Deployment
    target, reason = app._rollout_target(
        Pod(name="raw-x", namespace="default",
            owner_kind="ReplicaSet", owner_name="raw"))
    assert target is None and "raw" in reason

    # a non-hash suffix (canary RS, Argo Rollouts, etc.) must NOT be mistaken
    # for a Deployment-managed RS — the ReplicaSet keeps its own identity
    target, reason = app._rollout_target(
        Pod(name="web-canary-xyzzy", namespace="default",
            owner_kind="ReplicaSet", owner_name="web-canary"))
    assert target is None and "web-canary" in reason


def _pod_snapshot(pod):
    from kutop.model import Snapshot

    snap = Snapshot()
    snap.pods = [pod]
    return snap


def test_restart_gate_off_is_noop_warning() -> None:
    import asyncio

    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-7d4b9c6f9d-abcde", namespace="default",
                    node="node-a", phase="Running", ready="1/1",
                    owner_kind="ReplicaSet", owner_name="web-7d4b9c6f9d")))
            await pilot.pause()

            calls: list = []
            app._do_restart_rollout = (  # type: ignore[assignment]
                lambda target, ns: calls.append((target, ns)))
            notices: list = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append((msg, kw.get("severity"))))

            assert app.allow_destructive is False
            app.action_restart_pod()
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmModal)
            assert calls == []
            assert notices and notices[0][1] == "warning"
            assert "restart disabled" in notices[0][0]
            await pilot.exit(None)

    asyncio.run(drive())


def test_restart_warns_for_bare_and_job_owned_pods() -> None:
    import asyncio

    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        app = TopApp(["default"], allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            calls: list = []
            app._do_restart_rollout = (  # type: ignore[assignment]
                lambda target, ns: calls.append((target, ns)))
            notices: list = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append((msg, kw.get("severity"))))

            for pod in (
                Pod(name="solo", namespace="default", node="node-a",
                    phase="Running", ready="1/1"),
                Pod(name="migrate-abc", namespace="default", node="node-a",
                    phase="Running", ready="1/1",
                    owner_kind="Job", owner_name="migrate"),
            ):
                notices.clear()
                app._apply_snapshot(_pod_snapshot(pod))
                await pilot.pause()
                app.action_restart_pod()
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmModal)
                assert notices and notices[0][1] == "warning"
                assert "restart unavailable" in notices[0][0]
                assert "delete (x)" in notices[0][0]
            assert calls == []
            await pilot.exit(None)

    asyncio.run(drive())


def test_restart_confirm_body_spells_full_identity() -> None:
    import asyncio

    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-a", allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-7d4b9c6f9d-abcde", namespace="default",
                    node="node-a", phase="Running", ready="1/1",
                    owner_kind="ReplicaSet", owner_name="web-7d4b9c6f9d")))
            await pilot.pause()

            calls: list = []
            app._do_restart_rollout = (  # type: ignore[assignment]
                lambda target, ns: calls.append((target, ns)))

            app.action_restart_pod()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            body = app.screen._body
            assert "context: ctx-a" in body
            assert "namespace: default" in body
            assert "pod: web-7d4b9c6f9d-abcde" in body
            assert "restarts: deployment/web" in body

            # confirming hands the resolved target to the kubectl runner
            app.screen.action_confirm()
            await pilot.pause()
            assert calls == [("deployment/web", "default")]
            await pilot.exit(None)

    asyncio.run(drive())


def test_restart_confirm_body_falls_back_to_current_context() -> None:
    import asyncio

    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        app = TopApp(["default"], allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="db-0", namespace="default", node="node-a",
                    phase="Running", ready="1/1",
                    owner_kind="StatefulSet", owner_name="db")))
            await pilot.pause()

            app.action_restart_pod()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            body = app.screen._body
            assert "context: current" in body
            assert "restarts: statefulset/db" in body
            app.screen.action_cancel()
            await pilot.exit(None)

    asyncio.run(drive())


def test_x_keypress_opens_restart_confirm() -> None:
    """End-to-end: pressing 'X' on a focused Deployment-owned pod (via RS)
    must push a ConfirmModal whose body mentions the rollout target."""
    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.widgets import ConfirmModal

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-b", allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-7d4b9c6f9d-abcde", namespace="default",
                    node="node-a", phase="Running", ready="1/1",
                    owner_kind="ReplicaSet", owner_name="web-7d4b9c6f9d")))
            await pilot.pause()

            # stub out the actual kubectl runner so no subprocess fires
            calls: list = []
            app._do_restart_rollout = (  # type: ignore[assignment]
                lambda target, ns: calls.append((target, ns)))

            await pilot.press("X")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            body = app.screen._body
            assert "deployment/web" in body

            # cancel — no kubectl should have run
            await pilot.press("n")
            await pilot.pause()
            assert calls == []
            await pilot.exit(None)

    asyncio.run(drive())


def test_restart_runner_invokes_kubectl_and_refreshes(monkeypatch) -> None:
    """_do_restart_rollout uses asyncio.create_subprocess_exec; exercise it
    with a fake subprocess that records the argv.  Two scenarios: returncode 0
    (success + refresh) and returncode 1 (failure notification)."""
    import asyncio as _asyncio

    from kutop.model import Pod
    from kutop.render.app import TopApp

    # ── success scenario ──────────────────────────────────────────────────────
    captured_argv: list[list[str]] = []
    refresh_calls: list[int] = []
    notify_calls: list[tuple[str, str]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec_ok(*argv, stdout=None, stderr=None):
        captured_argv.append(list(argv))
        return FakeProc()

    async def drive_ok() -> None:
        app = TopApp(["default"], context="ctx-b", allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        app.refresh_snapshot = lambda: None  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec_ok)
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notify_calls.append((str(msg), kw.get("severity", ""))))
            app._request_refresh = lambda: refresh_calls.append(1)  # type: ignore[assignment]

            pod = Pod(name="web-7d4b9c6f9d-abcde", namespace="default",
                      node="node-a", phase="Running", ready="1/1",
                      owner_kind="ReplicaSet", owner_name="web-7d4b9c6f9d")
            target, _ = app._rollout_target(pod)
            assert target == "deployment/web"
            app._do_restart_rollout(target, "default")
            # let the fire-and-forget task settle
            for _ in range(20):
                await pilot.pause(0.05)
                if captured_argv:
                    break
            await pilot.pause()

            assert captured_argv, "create_subprocess_exec was not called"
            expected = app._restart_cmd(target, "default")
            assert captured_argv[0] == expected
            assert refresh_calls, "_request_refresh was not called after success"
            assert any("restarted" in m for m, _sev in notify_calls)
            await pilot.exit(None)

    asyncio.run(drive_ok())

    # ── failure scenario ──────────────────────────────────────────────────────
    fail_notify: list[tuple[str, str]] = []
    fail_refresh: list[int] = []

    class FailProc:
        returncode = 1

        async def communicate(self):
            return b"", b"boom"

    async def fake_exec_fail(*argv, stdout=None, stderr=None):
        return FailProc()

    async def drive_fail() -> None:
        app = TopApp(["default"], context="ctx-b", allow_destructive=True,
                     discover_namespaces=False, auto_refresh=False)
        app.refresh_snapshot = lambda: None  # type: ignore[assignment]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec_fail)
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: fail_notify.append((str(msg), kw.get("severity", ""))))
            app._request_refresh = lambda: fail_refresh.append(1)  # type: ignore[assignment]

            app._do_restart_rollout("deployment/web", "default")
            for _ in range(20):
                await pilot.pause(0.05)
                if fail_notify:
                    break
            await pilot.pause()

            assert fail_notify, "notify was not called on failure"
            assert any("restart failed" in m for m, _sev in fail_notify)
            assert fail_refresh, "_request_refresh was not called after failure"
            await pilot.exit(None)

    asyncio.run(drive_fail())


# ── pod YAML inspector (y) ────────────────────────────────────────────────────


def test_yaml_view_command_modes() -> None:
    from kutop.render.modals import YamlViewModal

    # with an explicit context the argv carries --context before the subcommand
    assert YamlViewModal._yaml_cmd("web-0", "default", "ctx-a") == [
        "kubectl", "--context", "ctx-a", "get", "pod", "web-0",
        "-n", "default", "-o", "yaml",
    ]
    # no context -> no --context flag
    assert YamlViewModal._yaml_cmd("solo", "ns1", None) == [
        "kubectl", "get", "pod", "solo", "-n", "ns1", "-o", "yaml",
    ]


def test_y_keypress_on_focused_pod_pushes_yaml_modal() -> None:
    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.modals import YamlViewModal

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-b",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-0", namespace="default", node="node-a",
                    phase="Running", ready="1/1")))
            await pilot.pause()

            depth = len(app.screen_stack)
            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, YamlViewModal)
            assert len(app.screen_stack) == depth + 1
            assert app.screen.pod_name == "web-0"
            assert app.screen.ns == "default"
            assert app.screen.context == "ctx-b"
            await pilot.exit(None)

    asyncio.run(drive())


def test_y_with_no_focused_pod_notifies_and_pushes_nothing() -> None:
    from kutop.render.app import TopApp
    from kutop.render.modals import YamlViewModal

    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            notices: list = []
            app.notify = (  # type: ignore[assignment]
                lambda msg, **kw: notices.append((str(msg), kw.get("severity"))))

            depth = len(app.screen_stack)
            app.action_show_yaml()
            await pilot.pause()
            assert not isinstance(app.screen, YamlViewModal)
            assert len(app.screen_stack) == depth
            assert notices and notices[0][1] == "warning"
            assert "focus a pod row first" in notices[0][0]
            await pilot.exit(None)

    asyncio.run(drive())


def test_yaml_modal_escape_closes_without_arming_quit_or_clearing_search() -> None:
    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.modals import YamlViewModal

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-b",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-0", namespace="default", node="node-a",
                    phase="Running", ready="1/1")))
            await pilot.pause()
            # an active search term must survive the modal's close key
            app._search_term = "web"

            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, YamlViewModal)

            await pilot.press("escape")
            await pilot.pause()
            # escape closed the modal — and never reached app.clear_search
            assert not isinstance(app.screen, YamlViewModal)
            assert app._search_term == "web"
            # nor did q-driven quit arming bleed through (no q pressed, but the
            # event.stop() contract is what keeps the close key local)
            assert app._quit_hint_deadline == 0.0
            await pilot.exit(None)

    asyncio.run(drive())


# ── name filter: regex with substring fallback (key '/' and --filter) ─────────


def _filter_app(term: str):
    """A non-mounted TopApp with the live search term primed. _visible_pods is
    a pure method (reads only self._search_term / self.cfg / self._filter_cache)
    so it can be exercised without run_test, like the _shell_cmd test above."""
    from kutop.render.app import TopApp

    app = TopApp(namespaces=["default"], context="ctx-b",
                 discover_namespaces=False, auto_refresh=False)
    app._search_term = term
    # hide_completed defaults on; turn it off so the filter is the only gate.
    app.cfg.hide_completed = False
    app.cfg.only_problems = False
    return app


def _pods(*names: str):
    from kutop.model import Pod
    return [Pod(name=n, namespace="default", node="node-a",
                phase="Running", ready="1/1") for n in names]


def test_name_filter_plain_term_is_case_insensitive_substring() -> None:
    """(a) A plain term keeps the historical case-insensitive substring match."""
    app = _filter_app("WEB")
    visible = {p.name for p in app._visible_pods(_pods("web-0", "api-1", "Webhook-2"))}
    assert visible == {"web-0", "Webhook-2"}


def test_name_filter_regex_anchors_and_classes() -> None:
    """(b) A regex term matches via re.search; unrelated pods are dropped."""
    app = _filter_app("web-[0-9]+$")
    visible = {p.name for p in app._visible_pods(
        _pods("web-0", "web-12", "web-canary", "api-3"))}
    assert visible == {"web-0", "web-12"}
    # case-insensitivity carries through the regex path too
    app2 = _filter_app("^WEB")
    visible2 = {p.name for p in app2._visible_pods(_pods("web-0", "api-web"))}
    assert visible2 == {"web-0"}


def test_name_filter_invalid_regex_degrades_to_substring() -> None:
    """(c) An unbalanced pattern never raises — it falls back to substring."""
    app = _filter_app("web[")  # invalid regex (unterminated character class)
    # must not raise during render-equivalent filtering
    visible = {p.name for p in app._visible_pods(
        _pods("web[0]", "web-1", "api-2"))}
    # treated as the literal substring 'web[' → only the pod containing it
    assert visible == {"web[0]"}


def test_name_filter_compiled_matcher_is_cached(monkeypatch) -> None:
    """(d) The compiled matcher is memoized on the term: re.compile does not
    run again on a second render with the same term."""
    import re as _re

    app = _filter_app("web-[0-9]+$")
    calls = {"n": 0}
    real_compile = _re.compile

    def counting_compile(pattern, flags=0):
        calls["n"] += 1
        return real_compile(pattern, flags)

    monkeypatch.setattr(_re, "compile", counting_compile)

    pods = _pods("web-0", "api-1")
    first = {p.name for p in app._visible_pods(pods)}
    compiles_after_first = calls["n"]
    assert compiles_after_first >= 1  # the regex was compiled on first render
    second = {p.name for p in app._visible_pods(pods)}
    # cache hit: no further compilation on the identical term
    assert calls["n"] == compiles_after_first
    assert first == second == {"web-0"}


# ── ReDoS guard: nested-quantifier and over-length patterns ──────────────────


def test_term_is_regex_rejects_catastrophic_patterns() -> None:
    """Nested-quantifier shapes must be treated as plain substrings, not regex.

    Safe-looking patterns with metacharacters but no nested quantifiers remain
    True (regex path), while the catastrophic family is all False (substring).
    """
    from kutop.render.app import TopApp

    # catastrophic patterns -> False (falls back to substring)
    for bad in ("(a+)+$", "(.*)*", "(.+)+@", "(a{1,5})+"):
        assert TopApp._term_is_regex(bad) is False, (
            f"expected False for catastrophic pattern {bad!r}"
        )

    # safe regex patterns -> True
    for good in ("web-[0-9]+$", "^api", "[A-Z]ache"):
        assert TopApp._term_is_regex(good) is True, (
            f"expected True for safe regex pattern {good!r}"
        )


def test_has_nested_quantifier_white_box() -> None:
    """The catastrophic-backtracking detector must flag a quantifier applied to
    a group with an unbounded inner quantifier, and ONLY that — escaped parens
    are literals, an unquantified group is safe, and a quantifier at end of
    pattern (empty lookahead char) must not be misread as 'quantified'."""
    from kutop.render.app import TopApp as T

    # catastrophic: quantifier on a group holding an unbounded quantifier
    for bad in ("(a+)+", "(.*)*", "(.+)+@", "(a{1,5})+", "((a+)+)", "((a|b)*)+"):
        assert T._has_nested_quantifier(bad) is True, bad

    # safe: a group NOT quantified, or holding no unbounded quantifier, or
    # escaped parens (literals, not a group), or a top-level quantifier
    for ok in (r"\(a+\)+", "()+", "(a+)", "(ab)+", "a+",
               "web-(v[0-9]+)", "(api+)", "web-[0-9]+$", "(web|api)"):
        assert T._has_nested_quantifier(ok) is False, ok


def test_term_is_regex_rejects_over_length_term() -> None:
    """A term longer than _REGEX_MAX_LEN is treated as a plain substring."""
    from kutop.render.app import TopApp

    long_term = "a" * (TopApp._REGEX_MAX_LEN + 1)
    assert TopApp._term_is_regex(long_term) is False


def test_catastrophic_pattern_compile_filter_does_not_hang() -> None:
    """_compile_filter('(a+)+$') must return immediately (substring fallback).

    The nested-quantifier guard ensures the literal pattern is never handed to
    the regex engine on an adversarial input.  We verify both that the call
    returns and that the result is correct (the literal '(a+)+$' is not a
    substring of a string of 40 'a's followed by 'X').
    """
    app = _filter_app("(a+)+$")
    matcher = app._compile_filter("(a+)+$")
    adversarial_name = "a" * 40 + "X"
    # substring check: the literal '(a+)+$' is not in the name -> False
    result = matcher(adversarial_name)
    assert result is False


def test_over_length_term_compile_filter_uses_substring() -> None:
    """A term > _REGEX_MAX_LEN is matched as a plain substring, never as regex."""
    from kutop.render.app import TopApp

    long_term = "a" * (TopApp._REGEX_MAX_LEN + 1)
    app = _filter_app(long_term)
    matcher = app._compile_filter(long_term)
    # the long term itself IS a substring of a string containing it
    assert matcher("x" + long_term + "y") is True
    # and the regex-or-substring decision is recorded as substring (not regex)
    assert TopApp._term_is_regex(long_term) is False


# ── Display term not lowercased in empty-state message ────────────────────────


def test_empty_state_shows_verbatim_mixed_case_regex_term() -> None:
    """_effective_filter() must return the term as typed; the empty-state row
    must display it verbatim (not lowercased) so '[A-Z]' stays '[A-Z]'."""

    async def drive() -> None:
        from kutop.model import Pod, Snapshot
        from kutop.render.app import TopApp

        app = TopApp(["default"], context="ctx-b",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # load a pod so the table is populated, then set a filter that
            # matches nothing — forcing the empty-state row to appear
            snap = Snapshot()
            snap.pods = [Pod(name="api-99", namespace="default",
                             node="node-a", phase="Running", ready="1/1")]
            app._apply_snapshot(snap)
            await pilot.pause()

            # a mixed-case regex term that matches nothing in the snapshot
            app._search_term = "Web[A-Z]999"
            app.cfg.hide_completed = False
            app.cfg.only_problems = False
            await pilot.pause()

            msg = app._empty_state_message()
            # verbatim term preserved — not lowercased
            assert "Web[A-Z]999" in msg, (
                f"expected verbatim term in empty-state, got: {msg!r}"
            )
            assert "web[a-z]999" not in msg, (
                f"term was lowercased in empty-state message: {msg!r}"
            )
            await pilot.exit(None)

    asyncio.run(drive())


# ── YAML modal: q closes without arming quit or clearing search ───────────────


def test_yaml_modal_q_closes_without_arming_quit_or_clearing_search() -> None:
    """Pressing 'q' inside the YAML modal must dismiss it via event.stop(),
    never reaching the app's quit-arming binding.  The search term must survive
    and _quit_hint_deadline must remain 0.0."""
    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.modals import YamlViewModal

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-b",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-0", namespace="default", node="node-a",
                    phase="Running", ready="1/1")))
            await pilot.pause()
            # prime a search term that must survive
            app._search_term = "web"

            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, YamlViewModal), (
                "expected YamlViewModal to be pushed after 'y'"
            )

            await pilot.press("q")
            await pilot.pause()
            # modal dismissed by q
            assert not isinstance(app.screen, YamlViewModal), (
                "expected YamlViewModal to be dismissed after 'q'"
            )
            # search term survived
            assert app._search_term == "web", (
                f"search term was cleared: {app._search_term!r}"
            )
            # q did NOT arm the two-step quit (event.stop() kept it local)
            assert app._quit_hint_deadline == 0.0, (
                f"quit was armed: _quit_hint_deadline={app._quit_hint_deadline}"
            )
            await pilot.exit(None)

    asyncio.run(drive())


# ── Integration: YAML modal opened while a regex filter is active ─────────────


def test_yaml_modal_opens_with_active_regex_filter_unchanged() -> None:
    """Opening the YAML modal while a regex filter is active must not alter the
    search term.  The modal closes cleanly and the filter remains in place."""
    from kutop.model import Pod
    from kutop.render.app import TopApp
    from kutop.render.modals import YamlViewModal

    async def drive() -> None:
        app = TopApp(["default"], context="ctx-b",
                     discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._apply_snapshot(_pod_snapshot(
                Pod(name="web-0", namespace="default", node="node-a",
                    phase="Running", ready="1/1")))
            await pilot.pause()
            # a valid regex that matches the focused pod
            app._search_term = "web-[0-9]+"
            await pilot.pause()

            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, YamlViewModal), (
                "expected YamlViewModal after 'y' with active regex filter"
            )
            # filter must be unchanged while modal is open
            assert app._search_term == "web-[0-9]+", (
                f"search term changed on open: {app._search_term!r}"
            )

            # close the modal; filter still intact
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, YamlViewModal)
            assert app._search_term == "web-[0-9]+", (
                f"search term changed on close: {app._search_term!r}"
            )
            await pilot.exit(None)

    asyncio.run(drive())
