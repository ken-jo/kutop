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
