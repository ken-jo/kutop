"""Issue #12: bound kubectl fan-out during live refresh.

Covers the node /stats/summary TTL cache, the heavy/light enrich cadence with
re-attach + scope invalidation, the global concurrency cap, and the proxy
warning.
"""

from __future__ import annotations

import json
import threading
import time

from kutop.fetch import _MAX_CONCURRENCY, Fetcher
from kutop.model import Event, Node, Pod, PVC, Snapshot


# ── node /stats/summary TTL cache ─────────────────────────────────────────────


class _CountingRawFetcher(Fetcher):
    """Counts the per-node `--raw` stats/summary calls."""

    def __init__(self, namespaces, context=None):
        super().__init__(namespaces, context=context)
        self.raw_calls = 0

    def _run_optional(self, *args, timeout=0):
        if args[:2] == ("get", "--raw"):
            self.raw_calls += 1
            return json.dumps({"pods": []})
        return ""


def test_node_summaries_are_ttl_cached() -> None:
    f = _CountingRawFetcher(["default"])
    f._node_summaries(["n1", "n2"])
    assert f.raw_calls == 2            # cold: one --raw per node
    f._node_summaries(["n1", "n2"])
    assert f.raw_calls == 2            # within TTL: served from cache, no new calls
    # a newly-appearing node is fetched while the cached ones are reused
    f._node_summaries(["n1", "n2", "n3"])
    assert f.raw_calls == 3


def test_invalidate_caches_forces_node_refetch() -> None:
    f = _CountingRawFetcher(["default"])
    f._node_summaries(["n1"])
    assert f.raw_calls == 1
    f.invalidate_caches()
    f._node_summaries(["n1"])
    assert f.raw_calls == 2            # cache cleared -> refetched


def test_node_summary_cache_keyed_by_context() -> None:
    f = _CountingRawFetcher(["default"], context="ctx-a")
    f._node_summaries(["n1"])
    assert f.raw_calls == 1
    f.context = "ctx-b"               # a context switch must not serve ctx-a's payload
    f._node_summaries(["n1"])
    assert f.raw_calls == 2


# ── heavy / light enrich cadence ──────────────────────────────────────────────


class _CadenceFetcher(Fetcher):
    def __init__(self, namespaces):
        super().__init__(namespaces)
        self.events_calls = 0
        self.pvcs_calls = 0

    def _fetch_events(self):
        self.events_calls += 1
        return [Event(ts_utc="2026-01-01T00:00:00Z", name="e", reason="r",
                      message="m")]

    def _fetch_pvcs(self):
        self.pvcs_calls += 1
        return [PVC(name="p", namespace="default", capacity_mi=10)]

    def _node_summaries(self, node_names):
        return {}


def _snap_with_workload() -> Snapshot:
    snap = Snapshot()
    snap.nodes = [Node(name="n1", ready=True)]
    snap.pods = [Pod(name="pod-a", namespace="default", node="n1", phase="Running")]
    return snap


def test_heavy_cycle_fetches_light_cycle_reattaches() -> None:
    f = _CadenceFetcher(["default"])

    heavy = f.enrich_snapshot(_snap_with_workload(), heavy=True)
    assert (f.events_calls, f.pvcs_calls) == (1, 1)
    assert [e.name for e in heavy.events] == ["e"]
    assert [p.name for p in heavy.pvcs] == ["p"]

    # light cycle: no new events/PVC list calls, but the panels stay populated
    light = f.enrich_snapshot(_snap_with_workload(), heavy=False)
    assert (f.events_calls, f.pvcs_calls) == (1, 1)     # unchanged: skipped
    assert [e.name for e in light.events] == ["e"]      # re-attached from cache
    assert [p.name for p in light.pvcs] == ["p"]


def test_light_cycle_after_invalidate_shows_no_stale_data() -> None:
    f = _CadenceFetcher(["default"])
    f.enrich_snapshot(_snap_with_workload(), heavy=True)
    f.invalidate_caches()

    light = f.enrich_snapshot(_snap_with_workload(), heavy=False)
    # caches cleared on the scope switch: re-attach yields EMPTY, never the
    # previous cluster's events/PVCs
    assert light.events == []
    assert light.pvcs == []
    assert f.events_calls == 1  # still not refetched on a light cycle


# ── global concurrency cap ────────────────────────────────────────────────────


def test_run_caps_concurrent_kubectl_processes(monkeypatch) -> None:
    state = {"cur": 0, "peak": 0}
    lock = threading.Lock()

    class FakePopen:
        def __init__(self, *a, **k):
            self.returncode = 0

        def communicate(self, timeout=None):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.03)
            with lock:
                state["cur"] -= 1
            return ("ok", "")

        def kill(self):
            pass

    import kutop.fetch as fetch_mod
    monkeypatch.setattr(fetch_mod.subprocess, "Popen", FakePopen)

    f = Fetcher(["default"])
    threads = [threading.Thread(target=lambda: f._run_safe("get", "x"))
               for _ in range(_MAX_CONCURRENCY * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] <= _MAX_CONCURRENCY, (
        f"peak concurrency {state['peak']} exceeded cap {_MAX_CONCURRENCY}"
    )
    assert state["peak"] >= 2  # the cap actually allowed parallelism


# ── proxy warning ─────────────────────────────────────────────────────────────


def test_warn_if_proxied(monkeypatch, capsys) -> None:
    from kutop.cli import _warn_if_proxied

    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)

    _warn_if_proxied()
    assert capsys.readouterr().err == ""         # nothing set -> silent

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    _warn_if_proxied()
    err = capsys.readouterr().err
    assert "HTTPS_PROXY" in err and "NO_PROXY" in err


# ── app wiring: scope change forces a heavy cycle + clears caches ──────────────


def test_bump_fetch_gen_forces_heavy_and_invalidates() -> None:
    from kutop.render.app import TopApp

    app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
    invalidated = []
    app.fetcher.invalidate_caches = lambda: invalidated.append(True)  # type: ignore[method-assign]
    app._force_heavy = False

    app._bump_fetch_gen()

    assert app._force_heavy is True          # next fetch re-lists the heavy panels
    assert invalidated == [True]             # old-scope caches dropped
