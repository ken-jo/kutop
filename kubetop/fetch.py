"""kubectl data acquisition for kubetop.

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
    refresh — other nodes' results are preserved.
"""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import model
from .model import Node, Pod, PVC, Event, Summary, Snapshot

# Kept short so a quit mid-refresh can't hang the UI for long: the worker thread
# blocks in a kubectl call, and asyncio's shutdown joins that thread on exit.
# cancel() (below) kills in-flight processes for an immediate exit; these
# timeouts bound the worst case if a kill ever races a freshly-spawned process.
_KUBECTL_TIMEOUT = 6
_STATS_TIMEOUT = 4


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
        # Optional, profile-linked HTTP probes (M2/M3). Empty -> skipped entirely
        # (never touches the network), keeping --self-test kubectl/network-free.
        self.alertmanager_url = alertmanager_url or ""
        self.health_probes = list(health_probes or [])
        # Shutdown plumbing: cancel() kills any in-flight kubectl process so the
        # worker thread returns at once instead of blocking app/asyncio teardown.
        self._cancelled = threading.Event()
        self._procs: "set[subprocess.Popen]" = set()
        self._procs_lock = threading.Lock()

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
        proc = subprocess.Popen(
            self._base() + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._procs_lock:
            self._procs.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
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
        """Run kubectl, returning stdout or '' on any failure (never raises)."""
        try:
            return self._run(*args, timeout=timeout)
        except Exception:
            return ""

    def _probe_body(self, url: str, timeout: float):
        """Fetch an alert/health probe body.

        A ``/``-prefixed url is fetched through the Kubernetes API-server proxy
        via ``kubectl --raw`` — this reuses kubeconfig auth, so alerts/health work
        WITHOUT any localhost port-forward. http(s) urls use a direct request.
        Example alertmanager_url:
          /api/v1/namespaces/monitoring/services/<svc>:9093/proxy/api/v2/alerts
        """
        if url.startswith("/"):
            return self._run_safe("get", "--raw", url, timeout=_STATS_TIMEOUT) or None
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
        snap = Snapshot()
        try:
            nodes_by_name = self._fetch_nodes()
            snap.nodes = list(nodes_by_name.values())
        except Exception as exc:  # node fetch is foundational; surface but continue
            snap.error = f"nodes: {exc}"
            nodes_by_name = {}

        try:
            snap.pods = self._fetch_pods()
        except Exception as exc:
            snap.error = snap.error or f"pods: {exc}"

        # pod_count per node (from the pods we just listed in the target ns;
        # node objects may carry more from other namespaces but this is a useful
        # hint for the node row).
        for pod in snap.pods:
            if pod.node and pod.node in nodes_by_name:
                nodes_by_name[pod.node].pod_count += 1

        try:
            snap.events = self._fetch_events()
        except Exception as exc:
            snap.error = snap.error or f"events: {exc}"

        try:
            snap.pvcs = self._fetch_pvcs()
        except Exception as exc:
            snap.error = snap.error or f"pvcs: {exc}"

        # Kubelet stats summary (BUG FIX #2) drives both the cluster-wide PVC
        # panel usage AND per-pod storage attribution. Fetch each node's summary
        # ONCE (reused for both), then derive PVC usage + per-pod storage from
        # the cached payloads. Best effort: a node summary failure is isolated
        # and never crashes the refresh (PVC usage stays None / pod storage None).
        if snap.nodes and (snap.pvcs or snap.pods):
            try:
                summaries = self._node_summaries([n.name for n in snap.nodes])
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

        # Optional profile-linked HTTP probes. Alerts (AlertManager) are GENERIC
        # monitoring and stay in core. Each is robust: an unreachable/unset
        # endpoint yields empty data and never crashes the refresh.
        if self.alertmanager_url:
            try:
                from .probes import fetch_alerts
                snap.alerts = fetch_alerts(self.alertmanager_url, getter=self._probe_body)
            except Exception:
                snap.alerts = []

        # Domain-specific signals (e.g. workload health) come from optional
        # plugins. The core never imports a plugin module by name: it iterates the
        # plugin seam, letting each enabled plugin populate the snapshot. Guarded
        # so a missing/broken plugin (or the whole plugins package) never breaks
        # the refresh — the core runs fully without any plugin present.
        self._run_plugins(snap)

        snap.summary = self._build_summary(snap)
        return snap

    def _run_plugins(self, snap: Snapshot) -> None:
        """Let each enabled plugin populate ``snap`` (best effort, never raises).

        Uses the generic plugin seam (:func:`kubetop.plugins.iter_enabled`). A
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
            data = json.loads(gj)
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

        # Live usage from metrics-server.
        tn = self._run_safe("top", "nodes", "--no-headers")
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
        """Build Pod objects per namespace from `top pods` + `get pods -o json`."""
        pods: list[Pod] = []
        for ns in self.namespaces:
            # usage map: pod name -> (cpu_mcpu, mem_mi) summed across containers
            usage: dict[str, tuple[int, int]] = {}
            tp = self._run_safe("top", "pods", "-n", ns, "--no-headers", "--containers")
            if tp:
                for line in tp.splitlines():
                    parts = line.split()
                    # POD  NAME(container)  CPU  MEM
                    if len(parts) < 4:
                        continue
                    pod_name = parts[0]
                    cpu = model.to_mcpu(parts[2])
                    mem = model.to_mi(parts[3])
                    pc, pm = usage.get(pod_name, (0, 0))
                    usage[pod_name] = (pc + cpu, pm + mem)
            else:
                # fall back to pod-level top if --containers unsupported
                tp = self._run_safe("top", "pods", "-n", ns, "--no-headers")
                for line in tp.splitlines():
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    usage[parts[0]] = (model.to_mcpu(parts[1]), model.to_mi(parts[2]))

            gj = self._run_safe("get", "pods", "-n", ns, "-o", "json")
            if not gj:
                continue
            data = json.loads(gj)
            for item in data.get("items", []):
                pod = self._parse_pod(item, ns, usage)
                if pod is not None:
                    pods.append(pod)
        return pods

    def _parse_pod(
        self, item: dict, ns: str, usage: dict[str, tuple[int, int]]
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
                # an extra API call. We report kind as Deployment + the deploy name.
                owner_kind = "Deployment"
                owner_name = ref_name.rsplit("-", 1)[0] if "-" in ref_name else ref_name
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

        # requests/limits summed across all containers
        for c in spec.get("containers", []) or []:
            res = c.get("resources", {}) or {}
            req = res.get("requests", {}) or {}
            lim = res.get("limits", {}) or {}
            pod.cpu_req_mcpu += model.to_mcpu(str(req.get("cpu", "0")))
            pod.cpu_cap_mcpu += model.to_mcpu(str(lim.get("cpu", "0")))
            pod.mem_req_mi += model.to_mi(str(req.get("memory", "0")))
            pod.mem_cap_mi += model.to_mi(str(lim.get("memory", "0")))

        # container statuses: ready count, restarts, oom/crashloop detection
        cstatuses = status.get("containerStatuses", []) or []
        ready_n = 0
        total_n = len(cstatuses)
        restarts = 0
        oomkilled = False
        crashloop = False
        last_reason = ""
        for cs in cstatuses:
            if cs.get("ready"):
                ready_n += 1
            restarts += int(cs.get("restartCount", 0) or 0)
            last = (cs.get("lastState", {}) or {}).get("terminated", {}) or {}
            if last.get("reason") == "OOMKilled":
                oomkilled = True
            cur = (cs.get("state", {}) or {}).get("terminated", {}) or {}
            if cur.get("reason") == "OOMKilled":
                oomkilled = True
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            wreason = waiting.get("reason", "")
            if wreason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                crashloop = True
            # Capture the most relevant container reason for the optional
            # "last_reason" column: current waiting/terminated reason wins, else
            # the previous termination reason.
            last_reason = (
                last_reason
                or cur.get("reason", "")
                or wreason
                or last.get("reason", "")
            )
        pod.ready = f"{ready_n}/{total_n}" if total_n else "0/0"
        pod.restarts = restarts
        pod.oomkilled = oomkilled
        pod.crashloop = crashloop
        pod.last_terminated_reason = last_reason

        c_usage = usage.get(name, (0, 0))
        pod.cpu_mcpu = c_usage[0]
        pod.mem_mi = c_usage[1]
        return pod

    # ── events ───────────────────────────────────────────────────────────────
    def _fetch_events(self) -> list[Event]:
        events: list[Event] = []
        for ns in self.namespaces:
            gj = self._run_safe(
                "get", "events", "-n", ns, "--sort-by=.lastTimestamp", "-o", "json"
            )
            if not gj:
                continue
            data = json.loads(gj)
            for item in data.get("items", []):
                obj = item.get("involvedObject", {}) or {}
                events.append(
                    Event(
                        ts_utc=item.get("lastTimestamp")
                        or item.get("eventTime")
                        or item.get("firstTimestamp")
                        or "",
                        name=obj.get("name", "") or "",
                        reason=item.get("reason", "") or "",
                        message=(item.get("message", "") or "").replace("\n", " "),
                        count=int(item.get("count", 1) or 1),
                        type=item.get("type", "Normal") or "Normal",
                    )
                )
        # keep most recent first by timestamp string (ISO sorts lexically)
        events.sort(key=lambda e: e.ts_utc, reverse=True)
        return events

    # ── pvcs ─────────────────────────────────────────────────────────────────
    def _fetch_pvcs(self) -> list[PVC]:
        pvcs: list[PVC] = []
        for ns in self.namespaces:
            gj = self._run_safe("get", "pvc", "-n", ns, "-o", "json")
            if not gj:
                continue
            data = json.loads(gj)
            for item in data.get("items", []):
                meta = item.get("metadata", {})
                name = meta.get("name", "")
                if not name:
                    continue
                status = item.get("status", {})
                spec = item.get("spec", {})
                cap = (status.get("capacity", {}) or {}).get("storage", "0")
                pvcs.append(
                    PVC(
                        name=name,
                        namespace=ns,
                        capacity_mi=model.to_mi(str(cap)),
                        storage_class=spec.get("storageClassName", "") or "",
                    )
                )
        return pvcs

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
            out = self._run_safe(
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
        max_workers = max(1, min(8, len(node_names)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for node, stats in pool.map(node_summary, node_names):
                if stats is not None:
                    summaries[node] = stats
        return summaries

    def _fill_pvc_usage(self, pvcs: list[PVC], summaries: "dict[str, dict]") -> None:
        """Populate PVC.used_mi from the cached kubelet summaries.

        Maps each ``.pods[].volume[]`` entry's ``pvcRef.name`` -> ``usedBytes``
        across every node summary. A PVC with no matching volume keeps
        ``used_mi=None`` (renderer shows '-').
        """
        by_name: dict[str, PVC] = {p.name: p for p in pvcs}
        if not by_name:
            return
        for stats in summaries.values():
            for p in stats.get("pods", []) or []:
                for vol in p.get("volume", []) or []:
                    ref = vol.get("pvcRef") or {}
                    pname = ref.get("name")
                    if not pname:
                        continue
                    used = vol.get("usedBytes")
                    if used is None:
                        continue
                    pvc = by_name.get(pname)
                    if pvc is not None:
                        pvc.used_mi = int(used) // (1024 * 1024)  # bytes -> MiB

    def _fill_pod_storage(self, pods: list[Pod], summaries: "dict[str, dict]") -> None:
        """Attribute PVC-backed storage to each pod from the cached summaries.

        For every pod in a node summary we sum the ``usedBytes`` /
        ``capacityBytes`` of its PVC-backed volumes (volume entries carrying a
        ``pvcRef``) and assign them to the matching :class:`Pod` by
        (namespace, name). Pods with no PVC-backed volume stay
        ``storage_used_mi=None`` so a stateless pod renders as '-'. Failure
        isolated per pod entry; a malformed entry is skipped.
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
                for vol in p.get("volume", []) or []:
                    if not (vol.get("pvcRef") or {}).get("name"):
                        continue  # only PVC-backed volumes count toward pod storage
                    used = vol.get("usedBytes")
                    cap = vol.get("capacityBytes")
                    if used is None and cap is None:
                        continue
                    have_pvc = True
                    if used is not None:
                        used_total += int(used)
                    if cap is not None:
                        cap_total += int(cap)
                if have_pvc:
                    pod.storage_used_mi = used_total // (1024 * 1024)  # bytes -> MiB
                    pod.storage_cap_mi = cap_total // (1024 * 1024)

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
