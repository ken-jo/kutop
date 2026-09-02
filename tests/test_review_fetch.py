"""Regression tests for the 2026-09 fetch/model review fixes.

Covers, one test per defect:
  * init/ephemeral container status parsing (restarts, OOM, Init: prefix,
    pod-level status.reason, init names in container_names)
  * READY for a Pending pod (0/<spec containers>, not 0/0)
  * node /stats/summary scoped to the nodes that actually host watched pods
  * `-A` consolidation falling back to per-namespace on a TIMEOUT, and the
    total-namespace fraction guard on `_use_all_namespaces`
  * the duplicated `top pods` call (the --containers retry must be rare)
  * subprocess decoding of non-UTF-8 kubectl output
  * to_mcpu/to_mi on non-finite quantities
  * _probe_body honouring the caller's timeout
  * events.k8s.io `series` count / lastObservedTime
  * cached heavy-panel lists tagged with the scope they were fetched under

The kubectl seam is mocked exactly as the neighbouring suites do: subclasses
override ``_run`` (so the real _run_safe/_run_optional/fallback logic runs) or,
where the subprocess layer itself is under test, monkeypatch subprocess.Popen.
"""

from __future__ import annotations

import json

from kutop import model
from kutop.fetch import _ALL_NS_THRESHOLD, Fetcher
from kutop.model import Node, Pod, PVC, Snapshot


# ── 1. init / ephemeral containers ───────────────────────────────────────────


def _parse(item: dict, ns: str = "default") -> Pod:
    pod = Fetcher([])._parse_pod(item, ns, {})
    assert pod is not None
    return pod


def test_init_container_crashloop_is_reported_with_init_prefix() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}],
                 "initContainers": [{"name": "wait-for-db"}]},
        "status": {
            "phase": "Pending",
            "initContainerStatuses": [{
                "name": "wait-for-db", "ready": False, "restartCount": 7,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
            }],
        },
    })
    # kubectl's STATUS column shape: the init phase is called out explicitly
    assert pod.last_terminated_reason == "Init:CrashLoopBackOff"
    assert pod.crashloop is True
    assert pod.restarts == 7             # init restarts count toward the total
    assert pod.last_exit_code == 1


def test_init_container_oomkill_is_detected() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}],
                 "initContainers": [{"name": "migrate"}]},
        "status": {
            "phase": "Pending",
            "initContainerStatuses": [{
                "name": "migrate", "restartCount": 2,
                "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }],
        },
    })
    assert pod.oomkilled is True
    assert pod.last_terminated_reason == "Init:OOMKilled"
    assert pod.last_exit_code == 137


def test_completed_init_container_is_not_reported_as_a_failure() -> None:
    """Every healthy pod's init container terminates Completed/exit 0 — that
    must never surface as the pod's last failure reason."""
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}],
                 "initContainers": [{"name": "migrate"}]},
        "status": {
            "phase": "Running",
            "initContainerStatuses": [{
                "name": "migrate", "restartCount": 0,
                "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
            }],
            "containerStatuses": [{"name": "app", "ready": True,
                                   "restartCount": 0, "state": {"running": {}}}],
        },
    })
    assert pod.last_terminated_reason == ""
    assert pod.ready == "1/1"            # init containers never enter READY
    assert pod.crashloop is False


def test_running_pod_init_reason_is_not_init_prefixed() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}],
                 "initContainers": [{"name": "migrate"}]},
        "status": {
            "phase": "Running",
            "initContainerStatuses": [{
                "name": "migrate", "restartCount": 1,
                "state": {"terminated": {"reason": "Error", "exitCode": 2}},
            }],
            "containerStatuses": [{"name": "app", "ready": True,
                                   "restartCount": 0, "state": {"running": {}}}],
        },
    })
    # the init phase is history once the pod runs: no "Init:" prefix
    assert pod.last_terminated_reason == "Error"


def test_ephemeral_container_status_is_aggregated() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "app", "ready": True,
                                   "restartCount": 1, "state": {"running": {}}}],
            "ephemeralContainerStatuses": [{
                "name": "debugger", "restartCount": 3,
                "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }],
        },
    })
    assert pod.restarts == 4             # 1 regular + 3 ephemeral
    assert pod.oomkilled is True
    assert pod.ready == "1/1"            # ephemeral containers never enter READY


def test_pod_level_status_reason_is_used_when_no_container_reason() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}]},
        "status": {"phase": "Failed", "reason": "Evicted",
                   "message": "The node was low on resource: ephemeral-storage."},
    })
    assert pod.last_terminated_reason == "Evicted"


def test_container_names_list_regular_first_then_init() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}, {"name": "sidecar"}],
                 "initContainers": [{"name": "wait-for-db"}]},
        "status": {"phase": "Running"},
    })
    # index 0 must stay kubectl's default target (the first REGULAR container)
    assert pod.container_names == ["app", "sidecar", "wait-for-db"]


# ── 2. READY for Pending pods ────────────────────────────────────────────────


def test_pending_pod_ready_counts_spec_containers() -> None:
    pod = _parse({
        "metadata": {"name": "app-0"},
        "spec": {"containers": [{"name": "app"}, {"name": "sidecar"}]},
        "status": {"phase": "Pending"},          # no containerStatuses yet
    })
    assert pod.ready == "0/2"                    # kubectl's behaviour, not 0/0


def test_pod_with_no_spec_containers_stays_zero_of_zero() -> None:
    pod = _parse({"metadata": {"name": "app-0"}, "spec": {},
                  "status": {"phase": "Pending"}})
    assert pod.ready == "0/0"


# ── 3. node summary scoping ──────────────────────────────────────────────────


def test_node_summaries_only_cover_nodes_hosting_watched_pods() -> None:
    snap = Snapshot()
    snap.nodes = [Node(name=f"n{i}") for i in range(5)]
    snap.pods = [Pod(name="a", namespace="default", node="n1"),
                 Pod(name="b", namespace="default", node="n3"),
                 Pod(name="pending", namespace="default", node="")]
    assert Fetcher._nodes_to_summarize(snap) == ["n1", "n3"]


def test_node_summaries_fall_back_to_all_nodes_for_pvc_only_snapshot() -> None:
    snap = Snapshot()
    snap.nodes = [Node(name="n1"), Node(name="n2")]
    snap.pvcs = [PVC(name="data-0", namespace="default")]
    assert Fetcher._nodes_to_summarize(snap) == ["n1", "n2"]


def test_enrich_queries_only_hosting_nodes(monkeypatch) -> None:
    asked: list = []

    class _F(Fetcher):
        def _fetch_events(self):
            return []

        def _fetch_pvcs(self):
            return []

        def _node_summaries(self, node_names):
            asked.append(list(node_names))
            return {}

    snap = Snapshot()
    snap.nodes = [Node(name=f"n{i}") for i in range(3)]
    snap.pods = [Pod(name="a", namespace="default", node="n2")]
    _F(["default"]).enrich_snapshot(snap, heavy=True)
    assert asked == [["n2"]]


# ── 4. `-A` fallback on timeout + total-namespace fraction ───────────────────


class _TimeoutAllNsFetcher(Fetcher):
    """`-A` lists time out; the scoped per-namespace lists succeed."""

    def __init__(self, namespaces):
        super().__init__(namespaces)
        self.cmds: list = []

    def _run(self, *args, timeout=6):
        cmd = " ".join(args)
        self.cmds.append(cmd)
        if args[:1] == ("top",):
            raise RuntimeError("Metrics API not available")
        if "-A" in args:
            raise RuntimeError("timed out after 6s")
        if args[:2] == ("get", "pods"):
            ns = args[args.index("-n") + 1]
            return json.dumps({"items": [
                {"metadata": {"name": f"p-{ns}", "namespace": ns},
                 "spec": {}, "status": {"phase": "Running"}}]})
        if args[:2] == ("get", "events") or args[:2] == ("get", "pvc"):
            return json.dumps({"items": []})
        return ""


def test_timed_out_all_namespaces_list_falls_back_to_per_namespace() -> None:
    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    f = _TimeoutAllNsFetcher(ns)

    pods = f._fetch_pods()

    # a timeout must NOT blank the table: the scoped lists still deliver
    assert sorted(p.name for p in pods) == sorted(f"p-{n}" for n in ns)
    # ONE timeout is transient: the resource is not yet pinned to scoped lists
    assert "pods" not in f._all_ns_blocked
    assert f._all_ns_timeouts["pods"] == 1
    assert any("get pods -n ns0" in c for c in f.cmds)

    # the second consecutive timeout pins it for the session
    f._fetch_pods()
    assert "pods" in f._all_ns_blocked


def test_timed_out_all_namespaces_events_and_pvcs_fall_back() -> None:
    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    f = _TimeoutAllNsFetcher(ns)
    assert f._fetch_events() == []
    assert f._fetch_pvcs() == []
    assert not ({"events", "pvc"} & f._all_ns_blocked)   # first timeout: transient
    f._fetch_events()
    f._fetch_pvcs()
    assert {"events", "pvc"} <= f._all_ns_blocked          # second: pinned
    assert any("get events -n ns0" in c for c in f.cmds)
    assert any("get pvc -n ns0" in c for c in f.cmds)


def test_should_fall_back_to_scoped_covers_forbidden_and_timeout() -> None:
    assert Fetcher._should_fall_back_to_scoped("forbidden: cannot list at cluster scope")
    assert Fetcher._should_fall_back_to_scoped("timed out after 6s")
    # a genuine connectivity failure is not fixable by fanning out
    assert not Fetcher._should_fall_back_to_scoped("Unable to connect to the server")


def test_all_namespaces_needs_a_meaningful_share_of_the_cluster() -> None:
    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    f = Fetcher(ns)
    assert f._use_all_namespaces("pods")          # unknown total: legacy behaviour

    f.total_namespaces = 200                      # 4 of 200: `-A` pulls 98% waste
    assert not f._use_all_namespaces("pods")

    f.total_namespaces = _ALL_NS_THRESHOLD        # watching the whole cluster
    assert f._use_all_namespaces("pods")

    # a small/medium cluster keeps the 0.5.3 behaviour: threshold alone decides
    f.total_namespaces = 30                       # 4 of 30 -> still `-A`
    assert f._use_all_namespaces("pods")
    # a big cluster needs a meaningful share, not half of it
    f.total_namespaces = 60
    f.namespaces = [f"ns{i}" for i in range(10)]  # 10 of 60 (17%) -> `-A`
    assert f._use_all_namespaces("pods")
    f.namespaces = ns                              # 4 of 60 (7%) -> scoped
    assert not f._use_all_namespaces("pods")


def test_forbidden_all_namespaces_blocks_at_once() -> None:
    f = Fetcher([f"ns{i}" for i in range(_ALL_NS_THRESHOLD)])
    f._note_all_ns_failure("pods", "pods is forbidden: cannot list at cluster scope")
    assert "pods" in f._all_ns_blocked


def test_invalidate_caches_forgets_top_containers_verdict() -> None:
    f = Fetcher(["default"])
    f._top_containers_unsupported = True
    f._all_ns_timeouts["pods"] = 1
    f.invalidate_caches()
    assert f._top_containers_unsupported is False
    assert f._all_ns_timeouts == {}


def test_reason_and_exit_code_come_from_the_same_container() -> None:
    from kutop.fetch import _scan_container_statuses
    statuses = [
        # sidecar waiting in backoff, no previous termination recorded
        {"name": "sidecar", "state": {"waiting": {"reason": "CrashLoopBackOff"}},
         "lastState": {}},
        # app container terminated with exit 3
        {"name": "app", "state": {"terminated": {"reason": "Error", "exitCode": 3}}},
    ]
    scan = _scan_container_statuses(statuses)
    assert scan.reason == "CrashLoopBackOff"
    assert scan.exit_code is None          # not the OTHER container's 3


# ── 5. the duplicated `top pods` call ────────────────────────────────────────


class _TopCountingFetcher(Fetcher):
    """Counts `top pods` invocations; `get pods` returns one pod."""

    def __init__(self, namespaces, top_result=None, top_error=None):
        super().__init__(namespaces)
        self.top_cmds: list = []
        self._top_result = top_result
        self._top_error = top_error

    def _run(self, *args, timeout=6):
        if args[:1] == ("top",):
            self.top_cmds.append(" ".join(args))
            if self._top_error:
                raise RuntimeError(self._top_error)
            return self._top_result or ""
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": [
                {"metadata": {"name": "web", "namespace": "default"},
                 "spec": {}, "status": {"phase": "Running"}}]})
        return ""


def test_empty_top_output_does_not_trigger_a_second_top_call() -> None:
    """An empty namespace (or an absent metrics-server returning nothing) is not
    a --containers rejection: retrying doubles the kubectl calls for nothing."""
    f = _TopCountingFetcher(["default"], top_result="")
    f._fetch_pods_for_namespace("default")
    assert len(f.top_cmds) == 1
    assert "--containers" in f.top_cmds[0]


def test_failed_top_that_is_not_a_flag_rejection_does_not_retry() -> None:
    f = _TopCountingFetcher(["default"], top_error="Metrics API not available")
    f._fetch_pods_for_namespace("default")
    assert len(f.top_cmds) == 1


def test_unknown_containers_flag_retries_once_and_is_remembered() -> None:
    f = _TopCountingFetcher(["default"], top_error="unknown flag: --containers")
    f._fetch_pods_for_namespace("default")
    assert len(f.top_cmds) == 2                       # rejected, then pod-level
    assert "--containers" not in f.top_cmds[1]
    assert f._top_containers_unsupported is True

    f.top_cmds.clear()
    f._fetch_pods_for_namespace("default")
    # remembered: the flag is never offered again this session
    assert len(f.top_cmds) == 1
    assert "--containers" not in f.top_cmds[0]


def test_successful_top_is_parsed_without_a_second_call() -> None:
    f = _TopCountingFetcher(["default"], top_result="web app 120m 64Mi")
    pods = f._fetch_pods_for_namespace("default")
    assert len(f.top_cmds) == 1
    assert pods[0].cpu_mcpu == 120 and pods[0].mem_mi == 64


# ── 6. subprocess decoding ───────────────────────────────────────────────────


def test_run_decodes_non_utf8_output_without_raising(monkeypatch) -> None:
    """kubectl output is decoded as UTF-8 with errors='replace' regardless of the
    ambient locale, so a non-UTF-8 event message cannot fail the refresh."""
    import kutop.fetch as fetch_mod

    captured: dict = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            captured.update(kw)
            self.returncode = 0

        def communicate(self, timeout=None):
            # what the pipe would yield once decoded with errors='replace'
            return (b"caf\xe9".decode("utf-8", "replace"), "")

        def kill(self):
            pass

    monkeypatch.setattr(fetch_mod.subprocess, "Popen", FakePopen)
    out = Fetcher([])._run("get", "events")
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert out  # decoded, not an exception


# ── 7. non-finite quantities ─────────────────────────────────────────────────


def test_non_finite_quantities_yield_zero() -> None:
    for bad in ("inf", "-inf", "nan", "1e400", "1e400m"):
        assert model.to_mcpu(bad) == 0, bad
        assert model.to_mi(bad) == 0, bad
    assert model.to_mi("1e400Ki") == 0
    # sanity: the valid neighbours still parse
    assert model.to_mcpu("250m") == 250
    assert model.to_mi("1Gi") == 1024


# ── 8. probe timeout ─────────────────────────────────────────────────────────


def test_probe_body_honours_caller_timeout() -> None:
    seen: list = []

    class _F(Fetcher):
        def _run_optional(self, *args, timeout=6):
            seen.append(timeout)
            return "{}"

    f = _F([])
    f._probe_body("/healthz", 12.0)       # a longer budget is honoured
    f._probe_body("/healthz", 0)          # unset -> stats default
    f._probe_body("/healthz", 0.2)        # shorter than the default -> default
    from kutop.fetch import _STATS_TIMEOUT
    # the kubectl path includes a process spawn (+ exec credential plugin), so
    # a caller may lengthen the budget but never shrink it below the default
    assert seen == [12, _STATS_TIMEOUT, _STATS_TIMEOUT]


# ── 9. events.k8s.io series ──────────────────────────────────────────────────


def test_event_series_count_and_last_observed_time() -> None:
    ev = Fetcher._event_from_item({
        "involvedObject": {"name": "web-0"},
        "reason": "BackOff", "message": "Back-off restarting", "type": "Warning",
        "eventTime": "2026-01-01T00:00:00Z",
        "series": {"count": 42, "lastObservedTime": "2026-01-01T09:30:00Z"},
    })
    assert ev.count == 42                       # not the default 1
    assert ev.ts_utc == "2026-01-01T09:30:00Z"  # last sighting, not the first


def test_core_v1_event_shape_is_unchanged() -> None:
    ev = Fetcher._event_from_item({
        "involvedObject": {"name": "web-0"},
        "reason": "Killing", "message": "Stopping\ncontainer", "type": "Normal",
        "count": 3, "lastTimestamp": "2026-01-01T00:00:00Z",
    })
    assert (ev.count, ev.ts_utc) == (3, "2026-01-01T00:00:00Z")
    assert ev.message == "Stopping container"


# ── 10. cache scope tagging ──────────────────────────────────────────────────


class _ScopedCadenceFetcher(Fetcher):
    def __init__(self, namespaces):
        super().__init__(namespaces)
        self.events_calls = 0

    def _fetch_events(self):
        self.events_calls += 1
        return [model.Event(ts_utc="", name=f"e-{self.namespaces[0]}",
                            reason="r", message="m")]

    def _fetch_pvcs(self):
        return []

    def _node_summaries(self, node_names):
        return {}


def _snap() -> Snapshot:
    snap = Snapshot()
    snap.nodes = [Node(name="n1")]
    snap.pods = [Pod(name="p", namespace="default", node="n1")]
    return snap


def test_light_cycle_refetches_when_cache_belongs_to_another_scope() -> None:
    """The race this closes: a heavy enrich still in flight when
    invalidate_caches() runs re-caches the OLD scope's lists afterwards."""
    f = _ScopedCadenceFetcher(["old-ns"])
    f.enrich_snapshot(_snap(), heavy=True)
    assert f.events_calls == 1
    assert f._last_events and f._last_events[0].name == "e-old-ns"

    f.namespaces = ["new-ns"]              # scope switched; stale cache survived
    light = f.enrich_snapshot(_snap(), heavy=False)

    # the tag no longer matches: the light cycle is promoted to heavy for these
    # lists instead of re-attaching another namespace's events
    assert f.events_calls == 2
    assert [e.name for e in light.events] == ["e-new-ns"]


def test_light_cycle_still_reattaches_within_the_same_scope() -> None:
    f = _ScopedCadenceFetcher(["default"])
    f.enrich_snapshot(_snap(), heavy=True)
    light = f.enrich_snapshot(_snap(), heavy=False)
    assert f.events_calls == 1                     # no re-list
    assert [e.name for e in light.events] == ["e-default"]


def test_invalidated_cache_is_not_treated_as_another_scope() -> None:
    """invalidate_caches() drops the lists AND the tag; a light cycle then
    re-attaches the (empty) cache rather than paying for a re-list."""
    f = _ScopedCadenceFetcher(["default"])
    f.enrich_snapshot(_snap(), heavy=True)
    f.invalidate_caches()
    light = f.enrich_snapshot(_snap(), heavy=False)
    assert f.events_calls == 1
    assert light.events == []


# ── 11. type hygiene ─────────────────────────────────────────────────────────


def test_unknown_storage_capacity_is_none_and_renders_as_dash() -> None:
    pod = Pod(name="app-0", namespace="default")
    summaries = {"n1": {"pods": [
        {"podRef": {"namespace": "default", "name": "app-0"},
         "volume": [{"pvcRef": {"name": "data"}, "usedBytes": 1048576,
                     "capacityBytes": "GARBAGE"}]},
    ]}}
    Fetcher([])._fill_pod_storage([pod], summaries)
    assert pod.storage_cap_mi is None          # unknown, not a known 0
    assert pod.storage_pct is None            # falsy capacity -> renderer '-'
