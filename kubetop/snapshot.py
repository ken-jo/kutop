"""Headless one-frame SVG renderer for kubetop (M4).

Promotes the visual-QA harness (``tools/snapshot.py``) into a real product
feature: ``kubetop --snapshot PATH`` renders ONE live frame headlessly to an SVG
and exits. Works with live cluster data; falls back to a synthetic frame when a
fetch fails (so it always produces an artifact, even with no cluster).

Workload-agnostic by contract: the synthetic frame here uses ONLY generic names
(no namespace/pod literals) — workload-specific data only ever comes from a live
cluster + the profile YAML. ``tools/snapshot.py`` reuses :func:`render_snapshot`
so there is a single rendering code path.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .model import Node, Pod, PVC, Event, Snapshot, Summary


def synthetic_snapshot() -> Snapshot:
    """A rich, workload-agnostic synthetic frame (used when no cluster reachable).

    Uses generic node/pod/pvc names so this stays free of any workload literals
    (the de-hardcode invariant). Includes an OOMKilled pod, a pending pod, a
    warning event, and PVCs with/without usage to exercise the full renderer.
    """
    snap = Snapshot()
    snap.nodes = [
        Node(name="node-1", role="worker", cpu_mcpu=2900, cpu_cap_mcpu=8000,
             mem_mi=56000, mem_cap_mi=63000, pod_count=6, ready=True),
        Node(name="node-2", role="worker", cpu_mcpu=900, cpu_cap_mcpu=8000,
             mem_mi=22000, mem_cap_mi=63000, pod_count=4, ready=True),
    ]
    snap.pods = [
        # stateful pod with PVC-backed storage (USE/CAP populated)
        Pod(name="app-0", namespace="default", node="node-1", phase="Running",
            ready="1/1", cpu_mcpu=320, cpu_cap_mcpu=4000, mem_mi=7400,
            mem_cap_mi=16384, storage_used_mi=1000871, storage_cap_mi=3133440,
            start_time="2026-05-24T07:15:00Z"),
        Pod(name="worker-0", namespace="default", node="node-1", phase="Running",
            ready="1/1", restarts=1, oomkilled=True, cpu_mcpu=900,
            cpu_cap_mcpu=4000, mem_mi=12800, mem_cap_mi=16384,
            storage_used_mi=48000, storage_cap_mi=51200,
            start_time="2026-05-27T06:00:00Z"),
        # stateless pod: no PVC -> storage stays None (renders '-')
        Pod(name="pending-0", namespace="default", node="", phase="Pending",
            ready="0/1", start_time="2026-05-27T11:55:00Z"),
    ]
    snap.pvcs = [
        PVC(name="data-app-0", namespace="default", capacity_mi=3133440,
            used_mi=1000871, storage_class="gp3"),
        PVC(name="data-worker-0", namespace="default", capacity_mi=51200,
            used_mi=None, storage_class="gp3"),
    ]
    snap.events = [
        Event(ts_utc="2026-05-27T07:15:00Z", name="worker-0",
              reason="OOMKilling", message="Memory cgroup out of memory",
              count=3, type="Warning"),
    ]
    snap.summary = Summary(
        nodes_ready=2, nodes_total=2, pods_running=24, pods_pending=1,
        pods_failed=1, restarts_total=3, oomkilled_total=1, warn_events=5,
        cpu_used_mcpu=8757, cpu_cap_mcpu=28000,
        mem_used_mi=90963, mem_cap_mi=165375,
    )
    return snap


def _live_snapshot(namespaces, context=None, profile=None) -> Optional[Snapshot]:
    """Best-effort live cluster fetch (incl. profile-linked probes). None on fail."""
    from .fetch import Fetcher
    am = getattr(profile, "alertmanager_url", "") if profile else ""
    probes = getattr(profile, "health_probes", []) if profile else []
    try:
        snap = Fetcher(
            namespaces=namespaces, context=context,
            alertmanager_url=am, health_probes=probes,
        ).fetch()
    except Exception:
        return None
    if snap is None or snap.error:
        return None
    return snap


async def _render(out: str, size, namespaces, context, profile, app=None,
                  config=None) -> None:
    snap = _live_snapshot(namespaces, context=context, profile=profile) \
        or synthetic_snapshot()

    # Build the app if one wasn't supplied (CLI passes a pre-built app so the
    # snapshot reflects the user's full layered config; tools/ builds its own).
    # An explicit ``config`` (e.g. tools/ loading a temp config to enable an
    # opt-in column) is honoured so the snapshot reflects that column set.
    if app is None:
        from .render.app import TopApp
        from .config import Profile
        app = TopApp(
            namespaces=namespaces, interval=3.0,
            profile=profile or Profile(), context=context,
            config=config,
            discover_namespaces=False,
        )

    async with app.run_test(size=size) as pilot:
        # Feed several frames so the trend sparklines have history to draw.
        for _ in range(8):
            app._apply_snapshot(snap)
            await pilot.pause()
        app.save_screenshot(out)


def render_snapshot(
    out: str,
    size: "tuple[int, int]" = (200, 50),
    namespaces: "Optional[list[str]]" = None,
    context: Optional[str] = None,
    profile=None,
    app=None,
    config=None,
) -> int:
    """Render ONE frame headlessly to ``out`` (SVG) and return an exit code.

    Reuses the live cluster snapshot when reachable; otherwise a synthetic one.
    Returns 0 on success, 1 on an unexpected rendering failure (the artifact is
    still attempted). Safe to call from ``kubetop --snapshot`` or the tools harness.
    An optional ``config`` lets a caller pick the visible column set (e.g. the
    tools harness loading a temp config to surface an opt-in column).
    """
    nslist = namespaces or ["default"]
    try:
        asyncio.run(_render(out, size, nslist, context, profile, app=app,
                            config=config))
        return 0
    except Exception as exc:  # pragma: no cover - defensive
        import sys
        print(f"[snapshot] render failed: {exc}", file=sys.stderr)
        return 1
