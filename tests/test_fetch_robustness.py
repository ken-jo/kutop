"""Regression tests for the Fetcher robustness contract (0.4.1 audit fixes).

Covers: an unreachable cluster must surface Snapshot.error instead of silently
replacing the previous frame with an empty snapshot; optional calls
(metrics-server, kubelet stats) must NOT flag the refresh; PVC usage from the
kubelet summary must be keyed by (namespace, name), not name alone.
"""

from __future__ import annotations

import json

from kutop.fetch import Fetcher
from kutop.model import PVC


class UnreachableFetcher(Fetcher):
    """Every kubectl call fails, as with a dead/unauthorized cluster."""

    def _run(self, *args: str, timeout: int = 0) -> str:
        raise RuntimeError("Unable to connect to the server: dial tcp: timeout")


def test_unreachable_cluster_sets_snapshot_error() -> None:
    snap = UnreachableFetcher(["default"]).fetch_core()
    assert not snap.nodes and not snap.pods
    # the documented contract: any failure sets Snapshot.error so the renderer
    # keeps the previous frame instead of going blank without explanation
    assert snap.error
    assert "connect" in snap.error or "get" in snap.error


class MetricsLessFetcher(Fetcher):
    """API server reachable, metrics-server absent: top fails, get succeeds."""

    def _run(self, *args: str, timeout: int = 0) -> str:
        if args[0] == "top":
            raise RuntimeError("Metrics API not available")
        if args[:2] == ("get", "nodes"):
            return json.dumps({"items": [
                {"metadata": {"name": "n1", "labels": {}},
                 "status": {"capacity": {"cpu": "8", "memory": "32Gi"},
                            "conditions": [{"type": "Ready", "status": "True"}]}},
            ]})
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": []})
        return ""


def test_missing_metrics_server_is_not_a_refresh_error() -> None:
    snap = MetricsLessFetcher(["default"]).fetch_core()
    assert [n.name for n in snap.nodes] == ["n1"]
    assert snap.error == ""  # optional call: expected failure stays silent


class OneBadNamespaceFetcher(Fetcher):
    """Namespace 'broken' fails; namespace 'good' returns one pod."""

    def _run(self, *args: str, timeout: int = 0) -> str:
        joined = " ".join(args)
        if args[0] == "top":
            raise RuntimeError("Metrics API not available")
        if joined == "get nodes -o json":
            return json.dumps({"items": []})
        if joined == "get pods -n good -o json":
            return json.dumps({"items": [
                {"metadata": {"name": "web-1"}, "spec": {}, "status": {"phase": "Running"}},
            ]})
        if joined == "get pods -n broken -o json":
            raise RuntimeError("forbidden: User cannot list pods")
        return ""


def test_one_failing_namespace_keeps_partial_pods_and_reports_error() -> None:
    snap = OneBadNamespaceFetcher(["good", "broken"]).fetch_core()
    assert [p.name for p in snap.pods] == ["web-1"]
    assert "broken" in snap.error or "forbidden" in snap.error


class TwoBadNamespacesFetcher(Fetcher):
    """Both namespaces fail with DISTINCT errors; the API server is reachable."""

    def _run(self, *args: str, timeout: int = 0) -> str:
        joined = " ".join(args)
        if args[0] == "top":
            raise RuntimeError("Metrics API not available")
        if joined == "get nodes -o json":
            return json.dumps({"items": []})
        if joined == "get pods -n team-a -o json":
            raise RuntimeError("forbidden: User cannot list pods")
        if joined == "get pods -n team-b -o json":
            raise RuntimeError("dial tcp: i/o timeout")
        return ""


def test_every_distinct_failure_is_recorded_in_snapshot_errors() -> None:
    snap = TwoBadNamespacesFetcher(["team-a", "team-b"]).fetch_core()
    # both broken sources are individually diagnosable (namespace in the label)
    assert len(snap.errors) == 2
    assert any("team-a" in e and "forbidden" in e for e in snap.errors)
    assert any("team-b" in e and "timeout" in e for e in snap.errors)
    # .error keeps its historical contract: the single primary (first) failure
    assert snap.error == snap.errors[0]


def test_snapshot_errors_defaults_empty_and_error_stays_primary() -> None:
    from kutop.model import Snapshot

    blank = Snapshot()
    assert blank.error == "" and blank.errors == []

    snap = OneBadNamespaceFetcher(["good", "broken"]).fetch_core()
    assert snap.error  # unchanged single-failure behaviour
    assert snap.errors == [snap.error]


def test_concurrency_determinism_no_optional_leak() -> None:
    """fetch_core() run twice: required failures all present, optional 'top ...'
    failures absent, errors/error identical across both cycles (sorted
    determinism), and snap.error == snap.errors[0].
    """
    import time

    class JitteredFetcher(Fetcher):
        """_run sleeps a per-namespace amount then raises for three namespaces;
        top nodes / top pods go through _run_optional so their failures must
        never reach snap.errors."""

        # jitter table keyed on the ns arg (or '' for global calls)
        _SLEEP = {"team-a": 0.012, "team-b": 0.004, "team-c": 0.008}
        _FAIL_NS = {"team-a", "team-b", "team-c"}

        def _run(self, *args: str, timeout: int = 6) -> str:
            # Derive jitter: last arg for namespace-scoped calls, 0 otherwise
            ns = args[-1] if args and args[-1] in self._SLEEP else ""
            time.sleep(self._SLEEP.get(ns, 0.002))

            joined = " ".join(args)

            # optional calls — these will be intercepted by _run_optional's
            # thread-local sink, but we still raise so the sink gets exercised
            if args[0] == "top":
                raise RuntimeError("metrics-server unavailable")

            # this fake simulates a NAMESPACE-SCOPED-RBAC cluster: the cluster-
            # wide `-A` list is forbidden, so the fetcher falls back to the
            # per-namespace fan-out this test is exercising.
            if "-A" in args:
                raise RuntimeError("forbidden: cannot list at cluster scope")

            if joined == "get nodes -o json":
                return json.dumps({"items": []})

            # three required namespaces fail
            for ns_fail in self._FAIL_NS:
                if f"-n {ns_fail}" in joined:
                    raise RuntimeError(f"forbidden: {ns_fail}")

            # 'good' namespace succeeds
            if "get pods -n" in joined:
                return json.dumps({"items": []})

            return ""

    namespaces = ["team-a", "team-b", "team-c", "good"]
    fetcher = JitteredFetcher(namespaces)

    snap1 = fetcher.fetch_core()
    snap2 = fetcher.fetch_core()

    required_ns = {"team-a", "team-b", "team-c"}

    for snap in (snap1, snap2):
        # (a) all three required failures present in both cycles
        for ns in required_ns:
            assert any(ns in e for e in snap.errors), (
                f"expected failure for {ns} missing from snap.errors: {snap.errors}"
            )

        # (b) no optional 'top ...' failure leaked into snap.errors
        assert not any("metrics-server" in e or e.startswith("top ") for e in snap.errors), (
            f"optional 'top ...' failure leaked into snap.errors: {snap.errors}"
        )

        # (d) snap.error == snap.errors[0]
        assert snap.error == snap.errors[0], (
            f"snap.error {snap.error!r} != snap.errors[0] {snap.errors[0]!r}"
        )

    # (c) snap.errors identical across both cycles (sorted determinism)
    assert snap1.errors == snap2.errors, (
        f"errors not deterministic across cycles:\n  cycle1={snap1.errors}\n  cycle2={snap2.errors}"
    )
    assert snap1.error == snap2.error, (
        f"snap.error not deterministic: {snap1.error!r} vs {snap2.error!r}"
    )


def test_pvc_usage_is_keyed_by_namespace_and_name() -> None:
    # same claim name in two namespaces (same chart, two installs) — the
    # kubelet volume entry must update only the PVC in ITS namespace
    prod = PVC(name="data-0", namespace="prod", capacity_mi=1000)
    staging = PVC(name="data-0", namespace="staging", capacity_mi=1000)
    summaries = {
        "node-1": {"pods": [
            {"podRef": {"namespace": "prod", "name": "app-0"},
             "volume": [{"pvcRef": {"namespace": "prod", "name": "data-0"},
                         "usedBytes": 500 * 1024 * 1024}]},
        ]},
    }
    Fetcher([])._fill_pvc_usage([prod, staging], summaries)
    assert prod.used_mi == 500
    assert staging.used_mi is None  # unknown stays None, never cross-assigned


class UnparseablePodsFetcher(Fetcher):
    """API server reachable, nodes JSON valid, but the pods body is rc=0 garbage.

    kubectl exiting 0 with a truncated/garbage body must NOT yield an empty,
    error-free snapshot that silently replaces the previous good frame.
    """

    def _run(self, *args: str, timeout: int = 0) -> str:
        joined = " ".join(args)
        if args[0] == "top":
            raise RuntimeError("Metrics API not available")
        if joined == "get nodes -o json":
            return json.dumps({"items": []})
        if joined == "get pods -n default -o json":
            return "{ this is not valid json"  # rc=0, garbage body
        return ""


def test_unparseable_pods_json_is_recorded_not_silently_dropped() -> None:
    snap = UnparseablePodsFetcher(["default"]).fetch_core()
    assert snap.pods == []
    # the failure must surface (like the sibling nodes/events/pvcs fetchers) so
    # the renderer keeps the previous frame instead of going blank silently
    assert snap.error
    assert any("pods" in e and "default" in e for e in snap.errors)


def test_pvc_volume_garbage_usedBytes_yields_none_used_but_valid_cap() -> None:
    """A PVC volume whose usedBytes is non-numeric but whose capacityBytes is
    valid must leave storage_used_mi=None (unknown, not 0) while still
    reporting the correct storage_cap_mi.

    This pins the '_fill_pod_storage used_known / cap_known' fix: a parse
    failure on one field must not zero-out the other valid field.
    """
    from kutop.model import Pod

    pod = Pod(name="app-0", namespace="default")
    summaries = {
        "node-1": {"pods": [
            {"podRef": {"namespace": "default", "name": "app-0"},
             "volume": [{"pvcRef": {"name": "x"},
                         "usedBytes": "GARBAGE",
                         "capacityBytes": 1073741824}]},  # 1 GiB = 1024 MiB
        ]},
    }
    Fetcher([])._fill_pod_storage([pod], summaries)
    # valid cap is reported; unknown used stays None (never becomes 0)
    assert pod.storage_cap_mi == 1024, (
        f"expected storage_cap_mi=1024, got {pod.storage_cap_mi}"
    )
    assert pod.storage_used_mi is None, (
        f"expected storage_used_mi=None (unknown), got {pod.storage_used_mi}"
    )


def test_one_malformed_storage_entry_does_not_discard_other_pods_storage() -> None:
    # one volume with a non-numeric usedBytes precedes a valid pod's volume;
    # the bad entry is skipped in isolation, the valid pod's storage survives
    good = PVC(name="data-good", namespace="default", capacity_mi=1000)
    summaries = {
        "node-1": {"pods": [
            {"podRef": {"namespace": "default", "name": "bad-pod"},
             "volume": [{"pvcRef": {"namespace": "default", "name": "data-bad"},
                         "usedBytes": "not-a-number"}]},
            {"podRef": {"namespace": "default", "name": "good-pod"},
             "volume": [{"pvcRef": {"namespace": "default", "name": "data-good"},
                         "usedBytes": 200 * 1024 * 1024}]},
        ]},
    }
    # _fill_pvc_usage: malformed entry skipped, valid PVC still populated
    Fetcher([])._fill_pvc_usage([good], summaries)
    assert good.used_mi == 200

    # _fill_pod_storage: malformed pod's volume skipped, valid pod populated
    from kutop.model import Pod

    bad_pod = Pod(name="bad-pod", namespace="default")
    good_pod = Pod(name="good-pod", namespace="default")
    Fetcher([])._fill_pod_storage([bad_pod, good_pod], summaries)
    assert good_pod.storage_used_mi == 200
    # a non-numeric usedBytes never becomes a known 0 (None means unknown)
    assert bad_pod.storage_used_mi is None
