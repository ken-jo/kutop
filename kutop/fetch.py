"""kubectl data acquisition for kutop.

Produces a :class:`model.Snapshot` from a cluster. All kubectl invocations are
blocking ``subprocess.run`` calls, so :func:`fetch_snapshot` MUST be run off the
UI thread (the Textual app drives it via a ``@work(thread=True)`` worker and
pushes results back with ``call_from_thread``). This module never touches the
event loop and has no knowledge of any specific workload — ordering/timezone/
thresholds all come from the Profile, applied by the renderer.

Key robustness contracts:
  * Any failure sets ``Snapshot.error`` and returns a (possibly partial)
    snapshot; callers keep the previous frame on error.
  * PVC usage is sourced from the kubelet summary API per node
    (``/api/v1/nodes/<node>/proxy/stats/summary``) because metrics-server does
    NOT expose PVC usage. A single node's summary failure never aborts the
    refresh — other nodes' results are preserved. Only nodes that actually host
    one of the snapshot's pods are queried (a kubelet reports the volumes of
    its OWN pods only, so narrowing is lossless).
  * Every kubectl subprocess decodes as UTF-8 with ``errors="replace"``: event
    messages carry arbitrary bytes and a non-UTF-8 locale must never turn a
    refresh into a UnicodeDecodeError.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple, Optional

from . import model
from .model import Node, Pod, PVC, Event, Summary, Snapshot

# Kept short so a quit mid-refresh can't hang the UI for long: the worker thread
# blocks in a kubectl call, and asyncio's shutdown joins that thread on exit.
# cancel() (below) kills in-flight processes for an immediate exit; these
# timeouts bound the worst case if a kill ever races a freshly-spawned process.
_KUBECTL_TIMEOUT = 6
_STATS_TIMEOUT = 4

# Global ceiling on SIMULTANEOUS kubectl processes per fetcher. The per-namespace
# and per-node ThreadPoolExecutors each fan out, so without a shared cap a single
# refresh on a multi-namespace, multi-node cluster could spawn a dozen kubectl
# processes (and as many TLS/proxy CONNECTs) at once — the burst that makes a
# proxied workstation network feel unstable (issue #12). Queued calls wait for a
# slot; total work is unchanged, only the concurrency is bounded.
_MAX_CONCURRENCY = 4

# Node kubelet /stats/summary carries PVC + disk usage, which changes slowly.
# Re-listing it every 5s per node is the heaviest single source (each is an
# API-server `--raw` proxy call). Cache each node's payload for this many seconds
# so the per-pod storage column and PVC panel stay populated every cycle while
# the actual proxy calls happen ~once per TTL instead of every refresh.
_NODE_SUMMARY_TTL = 30.0

# Adaptive all-namespaces consolidation (issue #12 follow-up): when watching at
# least this many namespaces, list pods/events/PVCs with ONE cluster-wide
# `-A` call (filtered client-side) instead of one call per namespace — N calls
# collapse to 1. Below the threshold the scoped per-namespace calls are kept,
# because a single `-A` would pull the WHOLE cluster's objects (a much larger
# payload) to show only a few namespaces. A user without cluster-wide list RBAC
# falls back to per-namespace automatically (remembered for the session).
#
# The threshold alone is not enough on a BIG cluster: watching 4 of 200
# namespaces would pull all 200 namespaces' objects to render 4. So when the
# total namespace count is known (``Fetcher.total_namespaces``, set by the
# app's discovery worker) AND the cluster is large (at least
# _ALL_NS_FRACTION_MIN_TOTAL namespaces), `-A` additionally requires the
# watched set to be at least _ALL_NS_MIN_FRACTION of the cluster. On a
# small/medium cluster the threshold alone decides, exactly as in 0.5.3 — the
# guard only rejects the "thin slice of a huge cluster" case.
_ALL_NS_THRESHOLD = 4
_ALL_NS_FRACTION_MIN_TOTAL = 40
_ALL_NS_MIN_FRACTION = 0.15
# A cluster-wide list that TIMES OUT falls back to the scoped lists for that
# cycle; only this many consecutive timeouts pin the resource to the scoped
# path for the session (one 6s blip must not disable the consolidation).
_ALL_NS_TIMEOUTS_TO_BLOCK = 2


def current_context_name(context: Optional[str] = None) -> str:
    """The active kube context name, or '' if it cannot be determined.

    Returns an explicit ``context`` override when given; otherwise asks
    ``kubectl config current-context``. Standalone (no Fetcher needed) so the CLI
    can resolve the context before the app is built. Best-effort: any failure
    (kubectl absent, no current context) yields ''.
    """
    if context:
        return context
    try:
        proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, timeout=_KUBECTL_TIMEOUT,
            # explicit codec: a non-UTF-8 locale must not raise on decode
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


class Fetcher:
    """Stateless-ish kubectl fetcher. Holds connection params only.

    One instance can be reused across refreshes; ``namespaces`` and ``context``
    may be swapped between calls (the app does this for the ns switcher).
    """

    def __init__(
        self,
        namespaces: list[str],
        context: Optional[str] = None,
        alertmanager_url: str = "",
        health_probes: Optional[list] = None,
    ) -> None:
        self.namespaces = list(namespaces) or []
        self.context = context
        # Optional, profile-linked HTTP probes. Empty -> skipped entirely
        # (never touches the network), keeping --self-test kubectl/network-free.
        self.alertmanager_url = alertmanager_url or ""
        self.health_probes = list(health_probes or [])
        # Shutdown plumbing: cancel() kills any in-flight kubectl process so the
        # worker thread returns at once instead of blocking app/asyncio teardown.
        self._cancelled = threading.Event()
        self._procs: "set[subprocess.Popen]" = set()
        self._procs_lock = threading.Lock()
        # Failures recorded by _run_safe during one refresh (cleared by
        # fetch_core). Folded into Snapshot.error so an unreachable cluster
        # surfaces instead of silently yielding an empty, error-free snapshot.
        # Appends happen from parallel namespace/node worker threads, so every
        # access goes through _record_fetch_error / the lock; _run_optional
        # diverts its expected failures to a thread-local scratch sink.
        self._fetch_errors: "list[str]" = []
        self._errors_lock = threading.Lock()
        self._tl = threading.local()
        # Global concurrency cap (issue #12): bounds simultaneous kubectl procs.
        self._sem = threading.BoundedSemaphore(_MAX_CONCURRENCY)
        # Node /stats/summary TTL cache, keyed by (context, node) so a context
        # switch never serves another cluster's payload: (monotonic_ts, summary).
        self._summary_cache: "dict[tuple, tuple[float, Optional[dict]]]" = {}
        self._summary_cache_lock = threading.Lock()
        # Last heavy-cadence panel data, re-attached on the cheap core-only
        # cycles so events/PVC/alerts/health panels stay populated without
        # re-listing every namespace each refresh (see enrich_snapshot heavy=).
        self._last_events: "list[Event]" = []
        self._last_pvcs: "list[PVC]" = []
        self._last_alerts: list = []
        self._last_health: list = []
        # Scope the cached lists above were fetched under —
        # ``(tuple(namespaces), context)`` captured at the START of the heavy
        # enrich that produced them. ``None`` = no cached lists at all. A light
        # cycle only re-attaches them when the tag still matches the live scope
        # (see enrich_snapshot); this closes the race where a heavy enrich in
        # flight during invalidate_caches() re-caches the OLD scope's lists.
        self._cache_scope: Optional[tuple] = None
        # Resources whose cluster-wide `-A` list returned a forbidden/RBAC error
        # this session — fall back to per-namespace for them (remembered so we
        # don't pay the forbidden round-trip every refresh). Cleared on a scope
        # switch (context change may grant different RBAC).
        self._all_ns_blocked: "set[str]" = set()
        # consecutive `-A` timeouts per resource (see _note_all_ns_failure)
        self._all_ns_timeouts: "dict[str, int]" = {}
        # Total namespaces in the cluster, when the app/CLI knows it (from
        # list_namespaces()). None = unknown -> the `-A` decision uses the
        # watched-count threshold alone, i.e. unchanged legacy behaviour.
        self.total_namespaces: Optional[int] = None
        # Learned once per session: this server's `kubectl top pods` rejects
        # --containers, so the flag is dropped instead of paying a failed call
        # plus a retry on every namespace of every refresh.
        self._top_containers_unsupported = False

    def cancel(self) -> None:
        """Abort any in-flight (and future) kubectl calls. Idempotent, thread-safe.

        Called on app exit: sets a flag so no new process is spawned and kills
        every process currently running, unblocking the worker thread fast.
        """
        self._cancelled.set()
        with self._procs_lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    def invalidate_caches(self) -> None:
        """Drop all cross-refresh caches. Called on a scope (ns/context/profile)
        switch so a light cycle can never re-attach the previous cluster's
        events/PVCs or serve a stale node summary — the next fetch refills them.
        """
        with self._summary_cache_lock:
            self._summary_cache.clear()
        self._last_events = []
        self._last_pvcs = []
        self._last_alerts = []
        self._last_health = []
        self._cache_scope = None
        # a context switch may grant different RBAC — re-probe `-A` next time
        self._all_ns_blocked = set()
        self._all_ns_timeouts = {}
        # the next discovery re-counts the new cluster's namespaces
        self.total_namespaces = None
        # a different server may accept `top pods --containers` again
        self._top_containers_unsupported = False

    def _use_all_namespaces(self, resource: str) -> bool:
        """True when one cluster-wide ``-A`` list should replace the per-namespace
        fan-out for ``resource`` this cycle (see :data:`_ALL_NS_THRESHOLD`).

        Two conditions: enough watched namespaces for the fan-out to hurt, AND —
        when :attr:`total_namespaces` is known — a watched set large enough that
        one `-A` payload is not mostly objects we immediately discard.
        """
        if resource in self._all_ns_blocked:
            return False
        watched = len(self.namespaces)
        if watched < _ALL_NS_THRESHOLD:
            return False
        total = self.total_namespaces
        if (total and total >= _ALL_NS_FRACTION_MIN_TOTAL
                and watched < total * _ALL_NS_MIN_FRACTION):
            return False  # thin slice of a big cluster: scoped lists are cheaper
        return True

    def _note_all_ns_failure(self, resource: str, msg: str) -> None:
        """Record a failed `-A` list that is being retried as scoped lists.

        RBAC (forbidden) is a deterministic property of the session, so it
        blocks the resource at once. A timeout is transient: only
        :data:`_ALL_NS_TIMEOUTS_TO_BLOCK` consecutive ones block it.
        """
        if self._looks_forbidden(msg):
            self._all_ns_blocked.add(resource)
            return
        n = self._all_ns_timeouts.get(resource, 0) + 1
        self._all_ns_timeouts[resource] = n
        if n >= _ALL_NS_TIMEOUTS_TO_BLOCK:
            self._all_ns_blocked.add(resource)

    @staticmethod
    def _looks_forbidden(msg: str) -> bool:
        """Heuristic: did a list fail because of missing cluster-wide RBAC (so we
        should fall back to per-namespace) rather than a transient error?"""
        low = msg.lower()
        return "forbidden" in low or "cannot list" in low

    @classmethod
    def _should_fall_back_to_scoped(cls, msg: str) -> bool:
        """True when a failed cluster-wide ``-A`` list should be retried as the
        per-namespace fan-out instead of blanking the panel.

        Two cases: missing cluster-wide RBAC (``forbidden``), and a TIMEOUT —
        one `-A` list on a large cluster can easily exceed ``_KUBECTL_TIMEOUT``
        while the much smaller scoped lists each return in time. Treating the
        timeout as a hard failure showed an empty table for a cluster the user
        can perfectly well list.
        """
        return cls._looks_forbidden(msg) or "timed out" in msg.lower()

    @staticmethod
    def _containers_flag_rejected(msg: str) -> bool:
        """Did `top pods` fail because the server rejected ``--containers``?

        Only such a failure justifies the extra pod-level retry; a missing
        metrics-server or an RBAC error must not trigger a second call.
        """
        # kubectl's own wording: "unknown flag: --containers" / "unknown
        # shorthand flag". Deliberately NOT a bare "containers" substring — a
        # transient error that merely mentions containers would otherwise
        # disable per-container accounting for the rest of the session.
        low = msg.lower()
        return ("unknown flag" in low or "unknown shorthand" in low
                or "flag provided but not defined" in low)

    # ── low-level kubectl ────────────────────────────────────────────────────
    def _base(self) -> list[str]:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        return cmd

    def _run(self, *args: str, timeout: int = _KUBECTL_TIMEOUT) -> str:
        """Run kubectl, returning stdout. Raises on non-zero exit/timeout/cancel.

        Uses Popen (not subprocess.run) so the live process is tracked and can be
        killed by cancel() for an immediate quit.
        """
        if self._cancelled.is_set():
            raise RuntimeError("cancelled")
        # Hold a concurrency slot for the lifetime of the subprocess so a refresh
        # never runs more than _MAX_CONCURRENCY kubectl processes at once. A
        # thread blocked here for a slot still exits fast on quit: cancel() kills
        # the running procs, which frees slots, and the re-check below raises.
        with self._sem:
            if self._cancelled.is_set():
                raise RuntimeError("cancelled")
            proc = subprocess.Popen(
                self._base() + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Decode explicitly instead of following the ambient locale: an
                # event message may contain arbitrary non-ASCII bytes, and under
                # a non-UTF-8 locale the implicit decode raises
                # UnicodeDecodeError, failing the whole refresh over one event.
                encoding="utf-8",
                errors="replace",
            )
            with self._procs_lock:
                self._procs.add(proc)
            # Re-check after registering: cancel() may have snapshotted _procs in
            # the gap between the check above and the add, so its kill pass missed
            # this process — kill it ourselves instead of blocking up to `timeout`.
            if self._cancelled.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    # Bounded drain: kill() only signals kubectl itself, and an
                    # exec credential plugin grandchild can keep the stderr pipe
                    # open — an unbounded communicate() would block indefinitely.
                    proc.communicate(timeout=1)
                except Exception:
                    pass
                # Raise a concise message instead of TimeoutExpired, whose str()
                # dumps the whole kubectl argv ("Command '[...]' timed out") —
                # ugly in the error toast and full of brackets.
                raise RuntimeError(f"timed out after {timeout}s") from None
            finally:
                with self._procs_lock:
                    self._procs.discard(proc)
            if self._cancelled.is_set():
                raise RuntimeError("cancelled")
            if proc.returncode != 0:
                raise RuntimeError(
                    (err or out or f"kubectl {' '.join(args)} failed").strip()
                )
            return out

    def _run_safe(self, *args: str, timeout: int = _KUBECTL_TIMEOUT) -> str:
        """Run kubectl, returning stdout or '' on any failure (never raises).

        Failures are recorded into ``self._fetch_errors`` (cleared per refresh
        by :meth:`fetch_core`) so the snapshot can surface them as
        ``Snapshot.error`` — without this, an unreachable cluster yields an
        empty error-free snapshot that silently replaces the previous good
        frame. Calls whose failure is EXPECTED (metrics-server absent, kubelet
        stats, optional probes) go through :meth:`_run_optional` instead.
        """
        try:
            return self._run(*args, timeout=timeout)
        except Exception as exc:
            label = " ".join(list(args)[:3]) or "kubectl"
            # Namespace-scoped calls keep their namespace in the label so two
            # namespaces failing the same way (multi-ns RBAC) stay distinct in
            # Snapshot.errors instead of collapsing into one "get pods -n".
            if label.endswith(" -n") and len(args) > 3:
                label += f" {args[3]}"
            self._record_fetch_error(f"{label}: {exc}")
            return ""

    def _record_fetch_error(self, msg: str) -> None:
        """Record one failure, thread-safely.

        While a ``_run_optional`` call is active on this thread its failures
        land in a thread-local scratch list that is simply discarded — the old
        positional ``del`` from the shared list raced with the parallel
        namespace/node workers and destroyed (or leaked) their entries.
        """
        sink = getattr(self._tl, "sink", None)
        if sink is not None:
            sink.append(msg)
            return
        with self._errors_lock:
            self._fetch_errors.append(msg)

    def _run_optional(self, *args: str, timeout: int = _KUBECTL_TIMEOUT) -> str:
        """Best-effort variant of :meth:`_run_safe` for expected failures.

        Delegates to ``_run_safe`` (so tests overriding that single seam still
        intercept every kubectl call) but its failures stay silent: a missing
        metrics-server or an unreachable kubelet must not flag the whole
        refresh as failed. The diversion is a thread-local sink, never shared
        state, so parallel workers' real failures are untouched.
        """
        prev = getattr(self._tl, "sink", None)
        self._tl.sink = []
        try:
            return self._run_safe(*args, timeout=timeout)
        finally:
            self._tl.sink = prev

    def _run_optional_ok(
        self, *args: str, timeout: int = _KUBECTL_TIMEOUT
    ) -> "tuple[str, bool]":
        """:meth:`_run_optional` that also reports whether the call SUCCEEDED.

        Returns ``(stdout, True)`` on success and ``(error_message, False)`` on
        failure — an optional call swallows its error, so without this seam an
        empty result is indistinguishable from a failed one and callers end up
        retrying calls that simply had nothing to report.
        """
        prev = getattr(self._tl, "sink", None)
        sink: "list[str]" = []
        self._tl.sink = sink
        try:
            out = self._run_safe(*args, timeout=timeout)
        finally:
            self._tl.sink = prev
        if sink:
            return (sink[0], False)
        return (out, True)

    def _top_pods(self, *scope: str) -> str:
        """``kubectl top pods <scope> --no-headers`` output ('' when unavailable).

        Adds ``--containers`` (per-container rows sum to a pod total) unless
        this session already learned the server rejects the flag. The pod-level
        retry costs a WHOLE extra kubectl round trip per namespace per refresh,
        so it fires only when the first call actually failed BECAUSE of the flag
        — never when the namespace is simply empty or metrics-server is absent,
        both of which yield an empty body rather than a failure.
        """
        base = ("top", "pods") + tuple(scope) + ("--no-headers",)
        if not self._top_containers_unsupported:
            out, ok = self._run_optional_ok(*(base + ("--containers",)))
            if ok:
                return out
            if not self._containers_flag_rejected(out):
                return ""  # real failure (no metrics-server, RBAC, …): no retry
            self._top_containers_unsupported = True  # remembered for the session
        return self._run_optional(*base)

    def current_context_name(self) -> str:
        """Best-effort active kube context name for display ('' if unknown).

        Returns the explicit ``--context`` override when set, otherwise asks
        kubectl for the active context so the UI can show the real name instead
        of a generic "current".
        """
        if self.context:
            return self.context
        return self._run_optional("config", "current-context").strip()

    def _probe_body(self, url: str, timeout: float):
        """Fetch an alert/health probe body.

        A ``/``-prefixed url is fetched through the Kubernetes API-server proxy
        via ``kubectl --raw`` — this reuses kubeconfig auth, so alerts/health work
        WITHOUT any localhost port-forward. http(s) urls use a direct request.
        Example alertmanager_url:
          /api/v1/namespaces/monitoring/services/<svc>:9093/proxy/api/v2/alerts
        """
        if url.startswith("/"):
            # honour the caller's budget (probes.py passes the profile's own
            # timeout); fall back to the stats default when it is unset, and
            # never below 1s, which subprocess treats as "give up immediately".
            # never below the stats default: the call includes a process spawn
            # (+ exec credential plugin), so a caller may lengthen, not shrink it
            secs = max(int(timeout or 0), _STATS_TIMEOUT)
            return self._run_optional("get", "--raw", url, timeout=secs) or None
        from .probes import _http_get
        return _http_get(url, timeout)

    # ── live namespace discovery ─────────────────────────────────────────────
    def list_namespaces(self) -> list[str]:
        """Discover all cluster namespaces via ``kubectl get ns -o name``.

        Returns a sorted list of namespace names (the ``namespace/`` prefix is
        stripped). Raises on failure so callers can fall back to profile values.
        Reuses this fetcher's base/context handling for kubeconfig consistency.
        """
        out = self._run("get", "ns", "-o", "name")
        names: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # strip the "namespace/" prefix kubectl emits with `-o name`
            name = line.split("/", 1)[1] if "/" in line else line
            if name:
                names.append(name)
        return sorted(names)

    # ── public entrypoint ────────────────────────────────────────────────────
    def fetch(self) -> Snapshot:
        """Acquire one full snapshot. Safe to call from a worker thread."""
        snap = self.fetch_core()
        return self.enrich_snapshot(snap)

    def fetch_core(self) -> Snapshot:
        """Acquire the first-paint snapshot: nodes, pods, and summary only."""
        snap = Snapshot()
        with self._errors_lock:
            self._fetch_errors = []  # fresh refresh: previous cycle's errors are stale
        nodes_by_name: dict[str, Node] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            nodes_future = pool.submit(self._fetch_nodes)
            pods_future = pool.submit(self._fetch_pods)
            try:
                nodes_by_name = nodes_future.result()
                snap.nodes = list(nodes_by_name.values())
            except Exception as exc:  # node fetch is foundational; surface but continue
                snap.error = f"nodes: {exc}"
                snap.errors.append(snap.error)

            try:
                snap.pods = pods_future.result()
            except Exception as exc:
                snap.error = snap.error or f"pods: {exc}"
                snap.errors.append(f"pods: {exc}")

        # pod_count per node (from the pods we just listed in the target ns;
        # node objects may carry more from other namespaces but this is a useful
        # hint for the node row).
        for pod in snap.pods:
            if pod.node and pod.node in nodes_by_name:
                nodes_by_name[pod.node].pod_count += 1

        # Surface kubectl failures swallowed by _run_safe: with no data AND a
        # recorded error this is a full failure (caller keeps the previous
        # frame); with partial data the snapshot still applies, error attached.
        self._fold_fetch_errors(snap)

        snap.summary = self._build_summary(snap)
        return snap

    def _fold_fetch_errors(self, snap: Snapshot) -> None:
        """Fold ``self._fetch_errors`` into ``snap``. Idempotent.

        ``Snapshot.errors`` collects EVERY distinct failure of the cycle,
        SORTED so one persistent outage folds to the same list (and the same
        aggregated toast, which dedups on text) on every retry regardless of
        which worker thread happened to fail first. ``Snapshot.error`` stays
        the single primary failure (``errors[0]``) for backward compatibility,
        now deterministic for the same reason.
        """
        with self._errors_lock:
            msgs = list(self._fetch_errors)
        merged = sorted(set(snap.errors) | set(msgs))
        if not merged:
            return
        snap.errors = merged
        if not snap.error or snap.error in merged:
            snap.error = merged[0]

    def enrich_snapshot(self, snap: Snapshot, *, heavy: bool = True) -> Snapshot:
        """Fill slower auxiliary panels and storage details into ``snap``.

        ``heavy`` controls the cadence split (issue #12). On a HEAVY cycle the
        per-namespace events/PVC lists and the alert/health probes are fetched
        fresh and cached. On a LIGHT (core-cadence) cycle those re-list calls are
        skipped and the last heavy results are re-attached, so the panels stay
        populated without re-listing every namespace each 5s. Either way the node
        /stats/summary (TTL-cached) and the per-pod storage fill run every cycle,
        keeping the storage column current cheaply. ``invalidate_caches()`` clears
        the re-attach state on a scope switch so a light cycle can never show
        another cluster's data.

        The cached lists carry the scope they were fetched under. A light cycle
        re-attaches them ONLY while that tag still matches the live scope;
        otherwise it is promoted to a heavy cycle for those lists. Without the
        tag, a heavy enrich still in flight when ``invalidate_caches()`` runs
        would re-cache the OLD scope's events/PVCs after the clear, and the next
        light cycle would re-attach another namespace set's data.
        """
        # captured at the START: this is the scope the lists below are fetched
        # under, even if self.namespaces/context change while we run.
        scope = (tuple(self.namespaces), self.context)
        # A None tag means "no cached lists" (fresh fetcher / just invalidated):
        # re-attaching those empties is correct and costs nothing. Only a tag
        # from a DIFFERENT scope forces the light cycle to re-list.
        stale_cache = self._cache_scope is not None and self._cache_scope != scope
        heavy = heavy or stale_cache
        if heavy:
            with ThreadPoolExecutor(max_workers=2) as pool:
                events_future = pool.submit(self._fetch_events)
                pvcs_future = pool.submit(self._fetch_pvcs)
                try:
                    snap.events = events_future.result()
                except Exception as exc:
                    snap.error = snap.error or f"events: {exc}"
                    snap.errors.append(f"events: {exc}")

                try:
                    snap.pvcs = pvcs_future.result()
                except Exception as exc:
                    snap.error = snap.error or f"pvcs: {exc}"
                    snap.errors.append(f"pvcs: {exc}")
        else:
            # light cadence: reuse the last heavy fetch's lists (the PVCs already
            # carry their last-known usage; storage is re-filled below from the
            # TTL-cached node summaries so the per-pod column still tracks).
            snap.events = list(self._last_events)
            snap.pvcs = list(self._last_pvcs)

        # Kubelet stats summary drives both the cluster-wide PVC
        # panel usage AND per-pod storage attribution. Fetch each node's summary
        # ONCE (reused for both), then derive PVC usage + per-pod storage from
        # the cached payloads. Best effort: a node summary failure is isolated
        # and never crashes the refresh (PVC usage stays None / pod storage None).
        if snap.nodes and (snap.pvcs or snap.pods):
            try:
                summaries = self._node_summaries(self._nodes_to_summarize(snap))
            except Exception:
                summaries = {}
            if snap.pvcs:
                try:
                    self._fill_pvc_usage(snap.pvcs, summaries)
                except Exception:
                    pass  # keep capacities; renderer shows '-' for usage
            if snap.pods:
                try:
                    self._fill_pod_storage(snap.pods, summaries)
                except Exception:
                    pass  # leaves storage_used_mi=None; renderer shows '-'

        # Capacity fallback: a pod that declares a PVC but whose kubelet volume
        # stats were missing this cycle still shows its capacity (from the reliable
        # `kubectl get pvc`) as '-/CAP', so only truly stateless pods render '-'.
        if snap.pods and snap.pvcs:
            pvc_by_key = {(p.namespace, p.name): p for p in snap.pvcs}
            for pod in snap.pods:
                if pod.storage_cap_mi or not pod.pvc_claims:
                    continue
                cap = 0
                used = 0
                used_known = True
                matched = False
                for claim in pod.pvc_claims:
                    pvc = pvc_by_key.get((pod.namespace, claim))
                    if pvc is None:
                        continue
                    matched = True
                    cap += pvc.capacity_mi
                    if pvc.used_mi is None:
                        used_known = False
                    else:
                        used += pvc.used_mi
                if matched:
                    pod.storage_cap_mi = cap
                    if used_known:
                        pod.storage_used_mi = used

        if heavy:
            # Optional profile-linked HTTP probes. Alerts (AlertManager) are
            # GENERIC monitoring and stay in core. Each is robust: an
            # unreachable/unset endpoint yields empty data and never crashes the
            # refresh. These are network calls too, so they ride the heavy cadence.
            if self.alertmanager_url:
                try:
                    from .probes import fetch_alerts
                    snap.alerts = fetch_alerts(
                        self.alertmanager_url, getter=self._probe_body)
                except Exception:
                    snap.alerts = []

            # Domain-specific signals (e.g. workload health) come from optional
            # plugins. The core never imports a plugin module by name: it iterates
            # the plugin seam, letting each enabled plugin populate the snapshot.
            # Guarded so a missing/broken plugin (or the whole plugins package)
            # never breaks the refresh — the core runs fully without any plugin.
            self._run_plugins(snap)

            # cache this heavy cycle's panels for the next light cycles
            self._last_events = list(snap.events)
            self._last_pvcs = list(snap.pvcs)
            self._last_alerts = list(snap.alerts)
            self._last_health = list(snap.health)
            self._cache_scope = scope
        else:
            snap.alerts = list(self._last_alerts)
            snap.health = list(self._last_health)

        self._fold_fetch_errors(snap)

        snap.summary = self._build_summary(snap)
        return snap

    def _run_plugins(self, snap: Snapshot) -> None:
        """Let each enabled plugin populate ``snap`` (best effort, never raises).

        Uses the generic plugin seam (:func:`kutop.plugins.iter_enabled`). A
        plugin's activating config is carried on this fetcher (the app mirrors the
        unified Config's probe settings onto it), so we hand the fetcher itself as
        the config-like object. The whole block is guarded: if the plugins package
        is absent the core simply skips this step.
        """
        try:
            from .plugins import iter_enabled
        except Exception:
            return  # no plugins package -> core runs without any plugin
        for plugin in iter_enabled(self):
            try:
                plugin.fetch(self, snap)
            except Exception:
                continue  # a plugin must never crash the refresh

    # ── nodes ────────────────────────────────────────────────────────────────
    def _fetch_nodes(self) -> dict[str, Node]:
        """Build Node objects from `top nodes` + `get nodes -o json`."""
        nodes: dict[str, Node] = {}

        # Capacity / roles / readiness from the API object.
        gj = self._run_safe("get", "nodes", "-o", "json")
        if gj:
            try:
                data = json.loads(gj)
            except Exception:
                self._record_fetch_error("get nodes: unparseable kubectl output")
                data = {}
            for item in data.get("items", []):
                meta = item.get("metadata", {})
                name = meta.get("name", "")
                if not name:
                    continue
                status = item.get("status", {})
                cap = status.get("capacity", {})
                node = Node(name=name)
                node.cpu_cap_mcpu = model.to_mcpu(str(cap.get("cpu", "0")))
                node.mem_cap_mi = model.to_mi(str(cap.get("memory", "0")))
                node.role = _node_role(meta.get("labels", {}))
                node.ready = _node_ready(status.get("conditions", []))
                nodes[name] = node

        # Live usage from metrics-server (optional: its absence is expected
        # and handled by the startup preflight — never a refresh error).
        tn = self._run_optional("top", "nodes", "--no-headers")
        for line in tn.splitlines():
            parts = line.split()
            # NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
            if len(parts) < 5:
                continue
            name = parts[0]
            node = nodes.get(name) or Node(name=name)
            node.cpu_mcpu = model.to_mcpu(parts[1])
            node.mem_mi = model.to_mi(parts[3])
            nodes[name] = node

        return nodes

    # ── pods ─────────────────────────────────────────────────────────────────
    def _fetch_pods(self) -> list[Pod]:
        """Build Pod objects from `top pods` + `get pods -o json`.

        One namespace -> one scoped pair of calls. Many namespaces (>=
        :data:`_ALL_NS_THRESHOLD`) -> ONE cluster-wide ``-A`` pair filtered to
        the watched set, unless that is RBAC-forbidden, in which case we fall
        back to the per-namespace fan-out.
        """
        if not self.namespaces:
            return []
        if len(self.namespaces) == 1:
            return self._fetch_pods_for_namespace(self.namespaces[0])
        if self._use_all_namespaces("pods"):
            pods = self._fetch_pods_all()
            if pods is not None:
                return pods  # consolidated path succeeded
            # else: `-A` was forbidden -> _all_ns_blocked set, fall through
        pods = []
        max_workers = max(1, min(8, len(self.namespaces)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for ns_pods in pool.map(self._fetch_pods_for_namespace, self.namespaces):
                pods.extend(ns_pods)
        return pods

    @staticmethod
    def _pod_usage_all_ns(lines: str) -> "dict[tuple, tuple]":
        """Parse ``kubectl top pods -A`` output into ``{(ns, pod): (cpu, mem)}``.

        The ``-A`` layout leads with a NAMESPACE column; CPU/MEM are always the
        last two columns whether or not ``--containers`` adds a container column,
        so container rows sum cleanly per pod by indexing from the end.
        """
        usage: "dict[tuple, tuple]" = {}
        for line in lines.splitlines():
            parts = line.split()
            if len(parts) < 4:                 # NAMESPACE POD ... CPU MEM
                continue
            key = (parts[0], parts[1])
            pc, pm = usage.get(key, (0, 0))
            usage[key] = (pc + model.to_mcpu(parts[-2]),
                          pm + model.to_mi(parts[-1]))
        return usage

    def _fetch_pods_for_namespace(self, ns: str) -> list[Pod]:
        """Build Pod objects for one namespace."""
        pods: list[Pod] = []
        # usage map: (ns, pod) -> (cpu_mcpu, mem_mi) summed across containers
        tp = self._top_pods("-n", ns)
        usage: "dict[tuple, tuple]" = {}
        for line in tp.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            usage[(ns, parts[0])] = (
                usage.get((ns, parts[0]), (0, 0))[0] + model.to_mcpu(parts[-2]),
                usage.get((ns, parts[0]), (0, 0))[1] + model.to_mi(parts[-1]),
            )

        gj = self._run_safe("get", "pods", "-n", ns, "-o", "json")
        if not gj:
            return pods
        try:
            data = json.loads(gj)
        except Exception:
            # kubectl exited 0 with a truncated/garbage body: record it like the
            # sibling fetchers (_fetch_nodes/_fetch_events/_fetch_pvcs) so the
            # empty result is surfaced as a refresh error instead of silently
            # replacing the previous good frame with an empty, error-free table.
            self._record_fetch_error(f"get pods -n {ns}: unparseable kubectl output")
            return pods
        for item in data.get("items", []):
            pod = self._parse_pod(item, ns, usage)
            if pod is not None:
                pods.append(pod)
        return pods

    def _fetch_pods_all(self) -> "Optional[list[Pod]]":
        """One cluster-wide pods fetch (`top pods -A` + `get pods -A`), filtered
        to the watched namespaces. Returns the pod list, or ``None`` when the
        cluster-wide list is RBAC-forbidden OR timed out (the caller then falls
        back to the per-namespace fan-out, which may well succeed)."""
        watched = set(self.namespaces)
        usage = self._pod_usage_all_ns(self._top_pods("-A"))

        try:
            gj = self._run("get", "pods", "-A", "-o", "json")
        except Exception as exc:
            if self._should_fall_back_to_scoped(str(exc)):
                self._note_all_ns_failure("pods", str(exc))
                return None  # fall back to per-namespace (scoped RBAC / timeout)
            self._record_fetch_error(f"get pods -A: {exc}")
            return []
        try:
            data = json.loads(gj)
        except Exception:
            self._record_fetch_error("get pods -A: unparseable kubectl output")
            return []
        self._all_ns_timeouts.pop("pods", None)
        pods: list[Pod] = []
        for item in data.get("items", []):
            ns = (item.get("metadata", {}) or {}).get("namespace", "")
            if ns not in watched:
                continue
            pod = self._parse_pod(item, ns, usage)
            if pod is not None:
                pods.append(pod)
        return pods

    def _parse_pod(
        self, item: dict, ns: str, usage: "dict[tuple, tuple]"
    ) -> Optional[Pod]:
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        if not name:
            return None
        # Controlling owner: prefer the ownerReference with controller=true, else
        # the first listed. This both lets us skip one-shot Job pods (which clutter
        # the live view) and surface the pod's controller in the table.
        owner_refs = meta.get("ownerReferences", []) or []
        owner_ref = None
        for owner in owner_refs:
            if owner.get("controller"):
                owner_ref = owner
                break
        if owner_ref is None and owner_refs:
            owner_ref = owner_refs[0]

        owner_kind = ""
        owner_name = ""
        if owner_ref is not None:
            ref_kind = owner_ref.get("kind", "") or ""
            ref_name = owner_ref.get("name", "") or ""
            if ref_kind == "Job":
                # Skip one-shot Job pods (completed jobs, migrations, etc.).
                return None
            if ref_kind == "ReplicaSet":
                # Heuristic (standard, dependency-free): a ReplicaSet created by a
                # Deployment is named "<deploy>-<podTemplateHash>". Stripping the
                # trailing "-<hash>" segment surfaces the owning Deployment without
                # an extra API call — but only when the suffix actually looks like
                # a pod-template-hash; a standalone/CRD-managed ReplicaSet (e.g.
                # "web-canary") keeps its own kind+name so downstream actions
                # (rollout restart) can't target an unrelated Deployment "web".
                base, _, suffix = ref_name.rpartition("-")
                if base and model.pod_template_hash_like(suffix):
                    owner_kind = "Deployment"
                    owner_name = base
                else:
                    owner_kind = "ReplicaSet"
                    owner_name = ref_name
            else:
                # StatefulSet / DaemonSet / Job(handled above) / CRD controllers / …
                owner_kind = ref_kind
                owner_name = ref_name

        spec = item.get("spec", {})
        status = item.get("status", {})
        pod = Pod(name=name, namespace=ns)
        pod.owner_kind = owner_kind
        pod.owner_name = owner_name
        pod.node = spec.get("nodeName", "") or ""
        pod.phase = status.get("phase", "") or ""
        # A pod with a deletionTimestamp is Terminating (being replaced/evicted);
        # the renderer's hide_completed filter drops these so a dead pod doesn't
        # linger next to its live replacement.
        pod.terminating = bool(meta.get("deletionTimestamp"))
        # PVC claims this pod mounts — used for the capacity fallback so a stateful
        # pod still shows '-/CAP' when the kubelet volume stats are momentarily absent.
        pod.pvc_claims = [
            v["persistentVolumeClaim"]["claimName"]
            for v in (spec.get("volumes") or [])
            if isinstance(v, dict) and v.get("persistentVolumeClaim")
        ]
        # age: prefer the running status startTime, fall back to creationTimestamp
        pod.start_time = (
            status.get("startTime")
            or meta.get("creationTimestamp")
            or ""
        )

        # Container names in spec order, REGULAR containers first (index 0 stays
        # kubectl's default log/exec target), then init containers so the log
        # viewer can also pick an init container's logs — exactly where the
        # evidence lives when a pod is stuck in Init:CrashLoopBackOff.
        spec_containers = spec.get("containers", []) or []
        spec_init = spec.get("initContainers", []) or []
        pod.container_names = [
            str(c.get("name", "")) for c in list(spec_containers) + list(spec_init)
            if c.get("name")
        ]

        # requests/limits summed across all containers
        for c in spec_containers:
            res = c.get("resources", {}) or {}
            req = res.get("requests", {}) or {}
            lim = res.get("limits", {}) or {}
            pod.cpu_req_mcpu += model.to_mcpu(str(req.get("cpu", "0")))
            pod.cpu_cap_mcpu += model.to_mcpu(str(lim.get("cpu", "0")))
            pod.mem_req_mi += model.to_mi(str(req.get("memory", "0")))
            pod.mem_cap_mi += model.to_mi(str(lim.get("memory", "0")))

        # ── container statuses ───────────────────────────────────────────────
        # Restarts / OOM / backoff are aggregated over REGULAR + INIT +
        # EPHEMERAL containers (kubectl does the same): a pod wedged in
        # Init:CrashLoopBackOff or an OOMKilled init container is otherwise
        # invisible — it has no regular container status at all.
        cstatuses = status.get("containerStatuses", []) or []
        istatuses = status.get("initContainerStatuses", []) or []
        estatuses = status.get("ephemeralContainerStatuses", []) or []

        c_scan = _scan_container_statuses(cstatuses)
        i_scan = _scan_container_statuses(istatuses)
        e_scan = _scan_container_statuses(estatuses)
        # An init container that finished cleanly reports terminated
        # "Completed"/exit 0 on EVERY healthy pod, so its reason must never
        # become the pod's last-failure reason — rescan only the unfinished /
        # failed ones for the reason+exit code.
        i_failed = _scan_container_statuses(
            [cs for cs in istatuses if not _terminated_ok(cs)]
        )

        # READY counts REGULAR containers only, like kubectl's READY column.
        # A Pending pod has no containerStatuses yet: kubectl still shows
        # 0/<spec containers> ("0/2"), not the meaningless 0/0.
        ready_n = c_scan.ready
        total_n = len(cstatuses) if cstatuses else len(spec_containers)

        last_reason = c_scan.reason
        last_exit = c_scan.exit_code
        if not last_reason and i_failed.reason:
            # kubectl's STATUS column prefixes an init-phase failure with
            # "Init:" (Init:CrashLoopBackOff, Init:Error) — but only while the
            # pod has not reached Running, after which the init phase is history.
            last_reason = (i_failed.reason if pod.phase == "Running"
                           else "Init:" + i_failed.reason)
        if not last_reason and e_scan.reason:
            last_reason = e_scan.reason
        if not last_reason:
            # Pod-level reason (Evicted / NodeLost / Shutdown / …): the only
            # signal when the failure killed the pod before any container
            # status could record it.
            last_reason = status.get("reason", "") or ""
        if last_exit is None:
            last_exit = (i_failed.exit_code if i_failed.exit_code is not None
                         else e_scan.exit_code)

        pod.ready = f"{ready_n}/{total_n}"
        pod.restarts = c_scan.restarts + i_scan.restarts + e_scan.restarts
        pod.oomkilled = c_scan.oomkilled or i_scan.oomkilled or e_scan.oomkilled
        pod.crashloop = c_scan.backoff or i_scan.backoff or e_scan.backoff
        pod.last_terminated_reason = last_reason
        pod.last_exit_code = last_exit

        # usage is keyed by (namespace, name) so the same map serves both the
        # per-namespace `top` and the cluster-wide `top -A` paths.
        c_usage = usage.get((ns, name), (0, 0))
        pod.cpu_mcpu = c_usage[0]
        pod.mem_mi = c_usage[1]
        return pod

    # ── events ───────────────────────────────────────────────────────────────
    @staticmethod
    def _event_from_item(item: dict) -> Event:
        """One :class:`Event` from a core/v1 OR events.k8s.io API item.

        The newer events.k8s.io shape carries repetition in ``series``
        (``series.count`` / ``series.lastObservedTime``) instead of the core/v1
        ``count`` / ``lastTimestamp``; reading only the core fields showed every
        repeated event as a single occurrence stamped with its FIRST sighting.
        """
        obj = item.get("involvedObject", {}) or {}
        series = item.get("series") or {}
        count = item.get("count") or series.get("count") or 1
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
        return Event(
            ts_utc=series.get("lastObservedTime")
            or item.get("lastTimestamp")
            or item.get("eventTime")
            or item.get("firstTimestamp")
            or "",
            name=obj.get("name", "") or "",
            reason=item.get("reason", "") or "",
            message=(item.get("message", "") or "").replace("\n", " "),
            count=count,
            type=item.get("type", "Normal") or "Normal",
        )

    def _fetch_events(self) -> list[Event]:
        if self._use_all_namespaces("events"):
            evs = self._fetch_events_all()
            if evs is not None:
                return evs
        events: list[Event] = []
        for ns in self.namespaces:
            gj = self._run_safe(
                "get", "events", "-n", ns, "--sort-by=.lastTimestamp", "-o", "json"
            )
            if not gj:
                continue
            try:
                data = json.loads(gj)
            except Exception:
                # one namespace's garbage payload must not discard the events
                # already collected from the namespaces before it
                self._record_fetch_error(f"events[{ns}]: unparseable kubectl output")
                continue
            for item in data.get("items", []):
                events.append(self._event_from_item(item))
        # keep most recent first by timestamp string (ISO sorts lexically)
        events.sort(key=lambda e: e.ts_utc, reverse=True)
        return events

    def _fetch_events_all(self) -> "Optional[list[Event]]":
        """One cluster-wide events fetch filtered to the watched namespaces, or
        ``None`` when `-A` is RBAC-forbidden or timed out (caller falls back to
        per-namespace)."""
        watched = set(self.namespaces)
        try:
            gj = self._run("get", "events", "-A",
                           "--sort-by=.lastTimestamp", "-o", "json")
        except Exception as exc:
            if self._should_fall_back_to_scoped(str(exc)):
                self._note_all_ns_failure("events", str(exc))
                return None
            self._record_fetch_error(f"get events -A: {exc}")
            return []
        try:
            data = json.loads(gj)
        except Exception:
            self._record_fetch_error("get events -A: unparseable kubectl output")
            return []
        self._all_ns_timeouts.pop("events", None)
        events = [
            self._event_from_item(item)
            for item in data.get("items", [])
            if (item.get("metadata", {}) or {}).get("namespace", "") in watched
        ]
        events.sort(key=lambda e: e.ts_utc, reverse=True)
        return events

    # ── pvcs ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _pvc_from_item(item: dict, ns: str) -> "Optional[PVC]":
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        if not name:
            return None
        status = item.get("status", {})
        spec = item.get("spec", {})
        cap = (status.get("capacity", {}) or {}).get("storage", "0")
        return PVC(
            name=name,
            namespace=ns,
            capacity_mi=model.to_mi(str(cap)),
            storage_class=spec.get("storageClassName", "") or "",
        )

    def _fetch_pvcs(self) -> list[PVC]:
        if self._use_all_namespaces("pvc"):
            pvcs = self._fetch_pvcs_all()
            if pvcs is not None:
                return pvcs
        pvcs = []
        for ns in self.namespaces:
            gj = self._run_safe("get", "pvc", "-n", ns, "-o", "json")
            if not gj:
                continue
            try:
                data = json.loads(gj)
            except Exception:
                self._record_fetch_error(f"pvc[{ns}]: unparseable kubectl output")
                continue
            for item in data.get("items", []):
                pvc = self._pvc_from_item(item, ns)
                if pvc is not None:
                    pvcs.append(pvc)
        return pvcs

    def _fetch_pvcs_all(self) -> "Optional[list[PVC]]":
        """One cluster-wide PVC fetch filtered to the watched namespaces, or
        ``None`` when `-A` is RBAC-forbidden or timed out (caller falls back to
        per-namespace)."""
        watched = set(self.namespaces)
        try:
            gj = self._run("get", "pvc", "-A", "-o", "json")
        except Exception as exc:
            if self._should_fall_back_to_scoped(str(exc)):
                self._note_all_ns_failure("pvc", str(exc))
                return None
            self._record_fetch_error(f"get pvc -A: {exc}")
            return []
        try:
            data = json.loads(gj)
        except Exception:
            self._record_fetch_error("get pvc -A: unparseable kubectl output")
            return []
        self._all_ns_timeouts.pop("pvc", None)
        pvcs: list[PVC] = []
        for item in data.get("items", []):
            ns = (item.get("metadata", {}) or {}).get("namespace", "")
            if ns not in watched:
                continue
            pvc = self._pvc_from_item(item, ns)
            if pvc is not None:
                pvcs.append(pvc)
        return pvcs

    @staticmethod
    def _nodes_to_summarize(snap: Snapshot) -> "list[str]":
        """Node names whose kubelet ``/stats/summary`` can contribute to ``snap``.

        Narrowing this to the nodes that HOST one of the snapshot's pods is
        lossless: a kubelet only reports the volumes of the pods scheduled on
        it, so a node running none of ``snap.pods`` cannot carry storage for any
        pod or PVC we render — querying it is a pure cost (one API-server proxy
        call each). Watching a few namespaces on a 200-node cluster therefore
        costs a handful of proxy calls instead of 200.

        Fallback: when no watched pod is scheduled anywhere (no pods at all, or
        all Pending) but PVCs exist, the owning pods are unknown, so every node
        is queried — a PVC panel with no watched pods is rare and small.
        Documented trade-off: a PVC whose ONLY consumer is currently unscheduled
        (Pending, no node) shows '-' until that pod lands — no kubelet could
        report its usage anyway, so nothing real is lost.
        """
        known = [n.name for n in snap.nodes if n.name]
        hosting = {p.node for p in snap.pods if p.node}
        if not hosting:
            return known if snap.pvcs else []
        return [name for name in known if name in hosting]

    def _node_summaries(self, node_names: list[str]) -> "dict[str, dict]":
        """Fetch each node's kubelet ``/stats/summary`` payload once, in parallel.

        metrics-server does not expose PVC usage; the kubelet summary API does
        (``.pods[].volume[]`` entries carry ``pvcRef`` + ``usedBytes`` +
        ``capacityBytes``). Returns ``{node_name: parsed_summary_dict}`` for the
        nodes that responded — a node that errors/timeouts is simply omitted so
        other nodes' results still apply. The single payload feeds BOTH the
        cluster-wide PVC panel (:meth:`_fill_pvc_usage`) and per-pod storage
        attribution (:meth:`_fill_pod_storage`), so no extra kubectl is spent.
        """
        def node_summary(node: str) -> "tuple[str, Optional[dict]]":
            out = self._run_optional(
                "get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary",
                timeout=_STATS_TIMEOUT,
            )
            if not out:
                return (node, None)
            try:
                return (node, json.loads(out))
            except Exception:
                return (node, None)

        summaries: dict[str, dict] = {}
        if not node_names:
            return summaries

        # Serve fresh-enough nodes from the TTL cache; only fetch the rest. The
        # cache stores misses (None) too, so an unreachable node is not retried
        # every 5s either — it recovers within the TTL window.
        now = time.monotonic()
        to_fetch: list[str] = []
        with self._summary_cache_lock:
            for node in node_names:
                entry = self._summary_cache.get((self.context, node))
                if entry is not None and (now - entry[0]) < _NODE_SUMMARY_TTL:
                    if entry[1] is not None:
                        summaries[node] = entry[1]
                else:
                    to_fetch.append(node)
        if not to_fetch:
            return summaries

        max_workers = max(1, min(8, len(to_fetch)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(node_summary, to_fetch))
        stamp = time.monotonic()
        with self._summary_cache_lock:
            for node, stats in results:
                self._summary_cache[(self.context, node)] = (stamp, stats)
                if stats is not None:
                    summaries[node] = stats
        return summaries

    def _fill_pvc_usage(self, pvcs: list[PVC], summaries: "dict[str, dict]") -> None:
        """Populate PVC.used_mi from the cached kubelet summaries.

        Maps each ``.pods[].volume[]`` entry's ``pvcRef`` (namespace, name) ->
        ``usedBytes`` across every node summary — keyed like
        :meth:`_fill_pod_storage`, because PVC names are only unique per
        namespace (the same chart in two namespaces yields identical claim
        names). A PVC with no matching volume keeps ``used_mi=None``
        (renderer shows '-').
        """
        by_key: "dict[tuple[str, str], PVC]" = {
            (p.namespace, p.name): p for p in pvcs
        }
        if not by_key:
            return
        for stats in summaries.values():
            for p in stats.get("pods", []) or []:
                for vol in p.get("volume", []) or []:
                    ref = vol.get("pvcRef") or {}
                    pname = ref.get("name")
                    if not pname:
                        continue
                    used = _as_int(vol.get("usedBytes"))
                    if used is None:
                        continue  # missing or non-numeric usedBytes: skip this entry
                    pvc = by_key.get((ref.get("namespace", ""), pname))
                    if pvc is not None:
                        pvc.used_mi = used // (1024 * 1024)  # bytes -> MiB

    def _fill_pod_storage(self, pods: list[Pod], summaries: "dict[str, dict]") -> None:
        """Attribute PVC-backed storage to each pod from the cached summaries.

        For every pod in a node summary we sum the ``usedBytes`` /
        ``capacityBytes`` of its PVC-backed volumes (volume entries carrying a
        ``pvcRef``) and assign them to the matching :class:`Pod` by
        (namespace, name). Pods with no PVC-backed volume stay
        ``storage_used_mi=None`` so a stateless pod renders as '-'. Failure is
        isolated per volume entry: one non-numeric ``usedBytes``/``capacityBytes``
        skips only that entry (via :func:`_as_int`) instead of discarding storage
        for every pod in the cycle.
        """
        by_key: dict[tuple[str, str], Pod] = {(p.namespace, p.name): p for p in pods}
        if not by_key:
            return
        for stats in summaries.values():
            for p in stats.get("pods", []) or []:
                ref = p.get("podRef") or {}
                key = (ref.get("namespace", ""), ref.get("name", ""))
                pod = by_key.get(key)
                if pod is None:
                    continue
                used_total = 0
                cap_total = 0
                have_pvc = False
                # A counted PVC volume whose used/cap is missing or non-numeric
                # makes that total UNKNOWN, not 0 — reporting a parse failure as
                # a known 0 would violate the None-means-unknown invariant.
                used_known = True
                cap_known = True
                for vol in p.get("volume", []) or []:
                    if not (vol.get("pvcRef") or {}).get("name"):
                        continue  # only PVC-backed volumes count toward pod storage
                    used = _as_int(vol.get("usedBytes"))
                    cap = _as_int(vol.get("capacityBytes"))
                    if used is None and cap is None:
                        continue  # missing or non-numeric both fields: skip entry
                    have_pvc = True
                    if used is None:
                        used_known = False
                    else:
                        used_total += used
                    if cap is None:
                        cap_known = False
                    else:
                        cap_total += cap
                if have_pvc:
                    pod.storage_used_mi = (
                        used_total // (1024 * 1024) if used_known else None
                    )
                    pod.storage_cap_mi = (
                        cap_total // (1024 * 1024) if cap_known else None
                    )

    # ── summary ──────────────────────────────────────────────────────────────
    def _build_summary(self, snap: Snapshot) -> Summary:
        s = Summary()
        s.nodes_total = len(snap.nodes)
        s.nodes_ready = sum(1 for n in snap.nodes if n.ready)
        for n in snap.nodes:
            s.cpu_used_mcpu += n.cpu_mcpu
            s.cpu_cap_mcpu += n.cpu_cap_mcpu
            s.mem_used_mi += n.mem_mi
            s.mem_cap_mi += n.mem_cap_mi
        for p in snap.pods:
            if p.phase == "Running":
                s.pods_running += 1
            elif p.phase == "Pending":
                s.pods_pending += 1
            elif p.phase == "Failed":
                s.pods_failed += 1
            s.restarts_total += p.restarts
            if p.oomkilled:
                s.oomkilled_total += 1
        s.warn_events = sum(1 for e in snap.events if e.type == "Warning")
        s.alerts_firing = len(snap.alerts)
        return s


# ── container-status helpers ─────────────────────────────────────────────────
# Waiting reasons that mean "this container is failing to start", whether it is
# a regular, init or ephemeral container.
_BACKOFF_REASONS = frozenset(("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"))


class _ContainerScan(NamedTuple):
    """Aggregate of one container-status list (see :func:`_scan_container_statuses`)."""
    ready: int = 0
    restarts: int = 0
    oomkilled: bool = False
    backoff: bool = False
    reason: str = ""
    exit_code: Optional[int] = None


def _scan_container_statuses(statuses: list) -> _ContainerScan:
    """Aggregate one ``*ContainerStatuses`` list.

    Shared by the regular, init and ephemeral lists so an init container's
    restarts, OOM kills and backoff states are diagnosed exactly like a regular
    container's. ``reason``/``exit_code`` describe the FIRST container with
    something to report: its current terminated/waiting reason wins, else the
    previous termination's.
    """
    ready = 0
    restarts = 0
    oomkilled = False
    backoff = False
    reason = ""
    exit_code: Optional[int] = None
    for cs in statuses or []:
        if not isinstance(cs, dict):
            continue
        if cs.get("ready"):
            ready += 1
        count = _as_int(cs.get("restartCount"))
        restarts += count if count and count > 0 else 0
        state = cs.get("state") or {}
        cur = state.get("terminated") or {}
        waiting = state.get("waiting") or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        if cur.get("reason") == "OOMKilled" or last.get("reason") == "OOMKilled":
            oomkilled = True
        wreason = waiting.get("reason", "") or ""
        if wreason in _BACKOFF_REASONS:
            backoff = True
        if not reason:
            # latch reason AND exit code from the SAME container so the pair
            # shown in the LAST REASON column ("OOMKilled(137)") really existed
            cur_reason = cur.get("reason", "") or ""
            if cur_reason:
                reason, exit_code = cur_reason, _as_int(cur.get("exitCode"))
            elif wreason:
                reason, exit_code = wreason, _as_int(last.get("exitCode"))
            elif last.get("reason"):
                reason, exit_code = last.get("reason", ""), _as_int(last.get("exitCode"))
    return _ContainerScan(ready, restarts, oomkilled, backoff, reason, exit_code)


def _terminated_ok(cs: dict) -> bool:
    """True when a container's CURRENT state is a clean termination (exit 0).

    Every healthy pod's init containers sit in exactly this state ("Completed",
    exit 0), so they must not contribute the pod's last-failure reason.
    """
    if not isinstance(cs, dict):
        return False
    term = (cs.get("state") or {}).get("terminated") or {}
    return bool(term) and _as_int(term.get("exitCode")) == 0


# ── parsing helpers ──────────────────────────────────────────────────────────
def _as_int(x: object) -> Optional[int]:
    """Coerce a kubelet byte field to int, or ``None`` if it isn't numeric.

    Used per volume entry so a single malformed ``usedBytes``/``capacityBytes``
    skips only that entry instead of discarding storage for every pod in the
    cycle. ``None`` (never a known 0) preserves the storage_used_mi/used_mi
    "None means unknown" invariant for a parse failure.
    """
    if x is None or isinstance(x, bool):
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ── node helpers ─────────────────────────────────────────────────────────────
def _node_role(labels: dict) -> str:
    """Derive a short role/group label from common node labels (generic)."""
    for key in (
        "node-role.kubernetes.io/control-plane",
        "node-role.kubernetes.io/master",
    ):
        if key in labels:
            return "control-plane"
    # cloud nodegroup labels (EKS/GKE/AKS) — generic best-effort
    for key in (
        "eks.amazonaws.com/nodegroup",
        "cloud.google.com/gke-nodepool",
        "agentpool",
        "node.kubernetes.io/instance-type",
    ):
        val = labels.get(key)
        if val:
            return str(val)
    return "worker"


def _node_ready(conditions: list) -> bool:
    for c in conditions or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False
