"""Issue #12 follow-up: adaptive all-namespaces (`-A`) list consolidation.

Below _ALL_NS_THRESHOLD namespaces -> scoped per-namespace calls (small
payload). At/above it -> one cluster-wide `-A` call filtered client-side, with
an automatic per-namespace fallback when `-A` is RBAC-forbidden.
"""

from __future__ import annotations

import json

from kutop.fetch import _ALL_NS_THRESHOLD, Fetcher


def _pod(ns: str, name: str) -> dict:
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {}, "status": {"phase": "Running"}}


def _event(ns: str, name: str) -> dict:
    return {"metadata": {"namespace": ns}, "involvedObject": {"name": name},
            "reason": "R", "message": "m", "type": "Warning",
            "lastTimestamp": "2026-01-01T00:00:00Z"}


def _pvc(ns: str, name: str) -> dict:
    return {"metadata": {"name": name, "namespace": ns},
            "status": {"capacity": {"storage": "1Gi"}}, "spec": {}}


class RecordingFetcher(Fetcher):
    """Records kubectl command lines and serves canned list payloads. Overrides
    only ``_run`` so the real _run_safe / _run_optional / filter / fallback
    logic is exercised."""

    def __init__(self, namespaces, by_ns, forbid_all=False):
        super().__init__(namespaces)
        self.by_ns = by_ns               # {ns: {"pods"|"events"|"pvc": [items]}}
        self.forbid_all = forbid_all
        self.cmds: list[str] = []

    def _run(self, *args, timeout=6):
        cmd = " ".join(args)
        self.cmds.append(cmd)
        if args and args[0] == "top":
            return ""                    # metrics absent (optional)
        if cmd == "get nodes -o json":
            return json.dumps({"items": []})
        res = ("pods" if args[:2] == ("get", "pods")
               else "events" if args[:2] == ("get", "events")
               else "pvc" if args[:2] == ("get", "pvc") else None)
        if res is None:
            return ""
        if "-A" in args:
            if self.forbid_all:
                raise RuntimeError("forbidden: cannot list at cluster scope")
            items = [it for ns in self.by_ns for it in self.by_ns[ns].get(res, [])]
            return json.dumps({"items": items})
        ns = args[args.index("-n") + 1]
        return json.dumps({"items": self.by_ns.get(ns, {}).get(res, [])})

    def _cmds_for(self, res):
        return [c for c in self.cmds if c.startswith(f"get {res} ")]


def test_below_threshold_uses_per_namespace() -> None:
    assert _ALL_NS_THRESHOLD >= 2
    ns = ["a", "b"]                      # below threshold
    by_ns = {"a": {"pods": [_pod("a", "pa")]}, "b": {"pods": [_pod("b", "pb")]}}
    f = RecordingFetcher(ns, by_ns)
    pods = f._fetch_pods()
    assert sorted(p.name for p in pods) == ["pa", "pb"]
    assert "get pods -A -o json" not in f.cmds          # no cluster-wide list
    assert "get pods -n a -o json" in f.cmds


def test_at_threshold_uses_single_filtered_all_namespaces_call() -> None:
    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    by_ns = {n: {"pods": [_pod(n, f"p-{n}")]} for n in ns}
    # a namespace the user is NOT watching: must be filtered out of the -A result
    by_ns["unwatched"] = {"pods": [_pod("unwatched", "secret")]}
    f = RecordingFetcher(ns, by_ns)

    pods = f._fetch_pods()

    assert sorted(p.name for p in pods) == sorted(f"p-{n}" for n in ns)
    assert "secret" not in {p.name for p in pods}       # filtered client-side
    assert f.cmds.count("get pods -A -o json") == 1     # ONE call, not N
    assert not any("get pods -n" in c for c in f.cmds)  # no per-namespace fan-out


def test_forbidden_all_namespaces_falls_back_and_is_remembered() -> None:
    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    by_ns = {n: {"pods": [_pod(n, f"p-{n}")]} for n in ns}
    f = RecordingFetcher(ns, by_ns, forbid_all=True)

    pods = f._fetch_pods()
    assert sorted(p.name for p in pods) == sorted(f"p-{n}" for n in ns)  # via fallback
    assert "pods" in f._all_ns_blocked
    assert any("get pods -n ns0" in c for c in f.cmds)  # per-namespace fallback ran

    f.cmds.clear()
    f._fetch_pods()
    # remembered: the second cycle does not even attempt `-A`
    assert not any("-A" in c for c in f._cmds_for("pods"))

    # a scope switch re-probes `-A`
    f.invalidate_caches()
    assert "pods" not in f._all_ns_blocked


def test_events_and_pvcs_consolidate_above_threshold() -> None:
    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    by_ns = {n: {"events": [_event(n, f"e-{n}")], "pvc": [_pvc(n, f"v-{n}")]}
             for n in ns}
    by_ns["unwatched"] = {"events": [_event("unwatched", "e-x")],
                          "pvc": [_pvc("unwatched", "v-x")]}
    f = RecordingFetcher(ns, by_ns)

    events = f._fetch_events()
    pvcs = f._fetch_pvcs()

    assert {e.name for e in events} == {f"e-{n}" for n in ns}   # 'e-x' filtered
    assert {v.name for v in pvcs} == {f"v-{n}" for n in ns}
    assert f.cmds.count("get pvc -A -o json") == 1
    assert sum(1 for c in f.cmds if c.startswith("get events -A")) == 1
    assert not any("get events -n" in c for c in f.cmds)
    assert not any("get pvc -n" in c for c in f.cmds)


def test_all_ns_pod_usage_is_keyed_by_namespace_and_name() -> None:
    # two namespaces with same-named pods: `top -A` rows must attribute usage to
    # the right (ns, pod), not collide on name
    class TopFetcher(RecordingFetcher):
        def _run(self, *args, timeout=6):
            if args[:1] == ("top",) and "-A" in args:
                self.cmds.append(" ".join(args))
                # NAMESPACE POD NAME(container) CPU MEM
                return "ns0 web app 100m 64Mi\nns1 web app 250m 128Mi"
            return super()._run(*args, timeout=timeout)

    ns = [f"ns{i}" for i in range(_ALL_NS_THRESHOLD)]
    by_ns = {"ns0": {"pods": [_pod("ns0", "web")]},
             "ns1": {"pods": [_pod("ns1", "web")]}}
    for n in ns[2:]:
        by_ns[n] = {"pods": []}
    f = TopFetcher(ns, by_ns)

    pods = {(p.namespace, p.name): p for p in f._fetch_pods()}
    assert pods[("ns0", "web")].cpu_mcpu == 100
    assert pods[("ns1", "web")].cpu_mcpu == 250
