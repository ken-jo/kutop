"""kubetop command-line entrypoint.

Backward compatible with ``top.sh <ns> <interval>``: positional ``namespaces``
(comma list) and ``interval`` are optional. A profile may supply default
namespaces and timezone; CLI overrides win. ``--self-test`` runs the app
headlessly with a synthetic snapshot (no kubectl) for cluster-independent CI.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import __version__
from .config import (
    Profile,
    SNAPSHOT_DETAIL_LEVELS,
    apply_detail_preset,
    dump_config_yaml,
    load_config,
    load_profile,
    snapshot_detail_size,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="kubetop",
        description="A modern btop-style Kubernetes resource dashboard for the terminal.",
    )
    ap.add_argument(
        "namespaces", nargs="?", default=None,
        help="comma-separated namespaces (default: from config/profile, else 'default')",
    )
    ap.add_argument(
        "interval", nargs="?", type=float, default=None,
        help="refresh interval in seconds (overrides config; default 3.0)",
    )
    ap.add_argument("--profile", default=None,
                    help="profile name (profiles/<name>.yaml) or explicit path")
    ap.add_argument("--config", default=None,
                    help="explicit config file path (default: ~/.config/kubetop/config.yaml)")
    ap.add_argument("--dump-config", action="store_true",
                    help="print the complete annotated config skeleton as YAML and exit")
    ap.add_argument("--tz", default=None,
                    help="IANA timezone for timestamps (overrides config; '' = local)")
    ap.add_argument("--context", default=None, help="kubeconfig context to use")
    ap.add_argument("--theme-accent", default=None,
                    help="accent color (cyan/green/magenta/blue/yellow/red/purple)")
    ap.add_argument("--sort", default=None,
                    help="initial sort key (priority/name/cpu/mem/cpu_pct/mem_pct/"
                         "restarts/phase/node/namespace/age)")
    ap.add_argument("--sort-desc", action="store_true",
                    help="reverse the initial sort direction")
    ap.add_argument("--summary-style", default=None, choices=("tiles", "compact"),
                    help="top summary header layout (tiles | compact)")
    ap.add_argument("--group-by-node", action="store_true",
                    help="group pods under their node (cluster topology view)")
    ap.add_argument("--filter", default=None,
                    help="initial pod name filter (case-insensitive substring)")
    ap.add_argument("--only-problems", action="store_true",
                    help="show only problem pods (non-Running / restarts>0 / oom)")
    ap.add_argument("--allow-destructive", action="store_true",
                    help="enable pod deletion (still gated by a confirm modal)")
    ap.add_argument("--log-tail", type=int, default=150,
                    help="lines of history for the live log viewer (default: 150)")
    ap.add_argument("--self-test", action="store_true",
                    help="run headlessly with a synthetic snapshot and exit (CI smoke test)")
    ap.add_argument("--snapshot", default=None, metavar="PATH",
                    help="render ONE live frame headlessly to an SVG at PATH and exit")
    ap.add_argument("--detail", choices=SNAPSHOT_DETAIL_LEVELS, default=None,
                    help="one-shot detail preset for columns (normal/wide/full)")
    ap.add_argument("--size", default=None, metavar="WxH",
                    help="terminal size for --snapshot (default depends on --detail)")
    ap.add_argument("--version", action="version",
                    version=f"kubetop {__version__}")
    return ap


def _parse_size(spec: str) -> "tuple[int, int]":
    """Parse a 'WxH' size spec into (width, height); default 140x40 on error."""
    try:
        w, h = spec.lower().split("x", 1)
        return (max(20, int(w)), max(10, int(h)))
    except (ValueError, AttributeError):
        return (140, 40)


def _snapshot_size(args) -> "tuple[int, int]":
    if args.size:
        return _parse_size(args.size)
    return snapshot_detail_size(args.detail)


def _base_overrides(args) -> dict:
    """Seed defaults from POSITIONAL args (namespaces / interval).

    Applied BELOW the saved user file so e.g. ``make top``'s ``TOP_NS`` seeds the
    first run, but the user's in-app namespace/interval choices persist across
    relaunches instead of being clobbered every time.
    """
    view: dict = {}
    cluster: dict = {}
    if args.interval is not None:
        view["interval"] = args.interval
    if args.namespaces:
        cluster["namespaces"] = [n.strip() for n in args.namespaces.split(",") if n.strip()]
    base: dict = {}
    if view:
        base["view"] = view
    if cluster:
        base["cluster"] = cluster
    return base


def _cli_overrides(args) -> dict:
    """Build a nested config-dict layer from explicit CLI FLAGS (override the file)."""
    view: dict = {}
    cluster: dict = {}
    filters: dict = {}
    if args.tz is not None:
        view["timezone"] = args.tz
    if args.theme_accent is not None:
        view["theme_accent"] = args.theme_accent
    if args.sort is not None:
        view["sort_key"] = args.sort
    if args.sort_desc:
        view["sort_desc"] = True
    if args.summary_style is not None:
        view["summary_style"] = args.summary_style
    if args.group_by_node:
        view["group_by_node"] = True
    if args.context is not None:
        cluster["context"] = args.context
    if args.filter is not None:
        filters["name_filter"] = args.filter
    if args.only_problems:
        filters["only_problems"] = True
    over: dict = {}
    if view:
        over["view"] = view
    if cluster:
        over["cluster"] = cluster
    if filters:
        over["filters"] = filters
    return over


def _self_test(app) -> int:
    """Headless render of one synthetic frame via Textual's test pilot."""
    import asyncio

    from .model import Node, Pod, PVC, Event, Snapshot

    snap = Snapshot()
    snap.nodes = [
        Node(name="node-a", role="worker", cpu_mcpu=2400, cpu_cap_mcpu=8000,
             mem_mi=12000, mem_cap_mi=32000, ready=True),
        Node(name="node-b", role="worker", cpu_mcpu=6000, cpu_cap_mcpu=8000,
             mem_mi=28000, mem_cap_mi=32000, ready=False),
    ]
    snap.pods = [
        # stateful pod with PVC-backed storage populated
        Pod(name="app-0", namespace="default", node="node-a", phase="Running",
            ready="1/1", cpu_mcpu=500, cpu_cap_mcpu=1000, mem_mi=800, mem_cap_mi=1024,
            storage_used_mi=15000, storage_cap_mi=51200,
            start_time="2026-05-20T07:15:00Z"),
        Pod(name="worker-9", namespace="default", node="node-b", phase="Running",
            ready="0/1", restarts=7, oomkilled=True,
            cpu_mcpu=950, cpu_cap_mcpu=1000, mem_mi=1000, mem_cap_mi=1024,
            start_time="2026-05-27T06:00:00Z"),
        # stateless pod: storage stays None
        Pod(name="pending-x", namespace="default", node="", phase="Pending", ready="0/1"),
    ]
    snap.pvcs = [
        PVC(name="data-app-0", namespace="default", capacity_mi=51200,
            used_mi=15000, storage_class="gp3"),
        PVC(name="data-worker-9", namespace="default", capacity_mi=51200,
            used_mi=None, storage_class="gp3"),
    ]
    snap.events = [
        Event(ts_utc="2026-05-27T12:34:56Z", name="worker-9", reason="OOMKilling",
              message="Memory cgroup out of memory", count=3, type="Warning"),
    ]

    # Build the summary the way the fetcher would.
    from .fetch import Fetcher
    snap.summary = Fetcher([], None)._build_summary(snap)

    async def drive() -> None:
        async with app.run_test() as pilot:
            app._apply_snapshot(snap)   # inject synthetic frame (no kubectl)
            await pilot.pause()
            assert app._loaded, "snapshot was not applied"
            assert app.cpu_hist, "cpu history empty after frame"
            assert app.query_one("#main_table").row_count > 0, "main table empty"
            app.fetcher.cancel()
            await pilot.exit(None)

    asyncio.run(drive())
    print("self-test OK: rendered 1 synthetic frame, no exception")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    profile = load_profile(args.profile) if args.profile else Profile()

    # Layer the unified config: defaults -> profile -> user file -> CLI flags.
    cfg = load_config(
        profile=profile,
        user_path=args.config,
        base_overrides=_base_overrides(args),
        cli_overrides=_cli_overrides(args),
    )
    apply_detail_preset(cfg, args.detail)

    # --dump-config: print the complete annotated skeleton and exit (no cluster).
    if args.dump_config:
        sys.stdout.write(dump_config_yaml(cfg))
        return 0

    # Lazy import so --version / --help / --dump-config don't require textual.
    from .render.app import TopApp

    app = TopApp(
        namespaces=list(cfg.namespaces),
        interval=cfg.interval,
        profile=profile,
        config=cfg,
        context=cfg.context or None,
        allow_destructive=args.allow_destructive,
        log_tail=args.log_tail,
        # --self-test / --snapshot must stay kubectl-free for discovery: skip
        # live namespace discovery (the snapshot still fetches its one frame).
        discover_namespaces=not (args.self_test or args.snapshot),
        auto_refresh=not (args.self_test or args.snapshot),
        force_color=bool(args.snapshot),
        config_path=args.config,
    )

    if args.self_test:
        return _self_test(app)

    # --snapshot: render one frame to an SVG and exit (reuses the built app so
    # the snapshot reflects the full layered config + profile-linked probes).
    if args.snapshot:
        from .snapshot import render_snapshot
        code = render_snapshot(
            args.snapshot,
            size=_snapshot_size(args),
            namespaces=list(cfg.namespaces),
            context=cfg.context or None,
            profile=profile,
            app=app,
        )
        if code == 0:
            print(f"wrote snapshot SVG -> {args.snapshot}")
        return code

    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
