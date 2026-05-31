#!/usr/bin/env python3
"""Headless screenshot harness for kubetop — renders one frame to SVG.

Thin wrapper around the in-package :func:`kubetop.snapshot.render_snapshot` (the
same code path as the ``kubetop --snapshot`` product feature). Fetches a live
cluster Snapshot when possible, otherwise falls back to a synthetic frame, and
writes an SVG. Kept for local visual QA/iteration.

Usage: python tools/snapshot.py OUT.svg [WIDTHxHEIGHT] [namespaces]
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kubetop.config import load_config, load_profile
from kubetop.snapshot import render_snapshot


def main() -> int:
    out = (sys.argv[1] if len(sys.argv) > 1
           else os.path.join(tempfile.gettempdir(), "kubetop.svg"))
    size = (200, 50)
    if len(sys.argv) > 2 and "x" in sys.argv[2]:
        w, h = sys.argv[2].split("x")
        size = (int(w), int(h))
    namespaces = (sys.argv[3].split(",") if len(sys.argv) > 3
                  else ["default"])
    # Optionally apply a profile (ordering + probes) when one is named via the
    # KUBETOP_SNAPSHOT_PROFILE env var; render_snapshot falls back to a generic
    # synthetic frame when no cluster. No workload literal is hardcoded here.
    profile = None
    prof_name = os.environ.get("KUBETOP_SNAPSHOT_PROFILE")
    if prof_name:
        try:
            profile = load_profile(prof_name)
        except Exception:
            profile = None
    # Optional: point KUBETOP_SNAPSHOT_CONFIG at a config file to control the
    # visible column set (e.g. enable an opt-in column like `owner`). Falls back
    # to the layered default (profile + defaults) when unset.
    config = None
    cfg_path = os.environ.get("KUBETOP_SNAPSHOT_CONFIG")
    if cfg_path:
        try:
            config = load_config(profile=profile, user_path=cfg_path)
            namespaces = list(config.namespaces) or namespaces
        except Exception:
            config = None
    code = render_snapshot(out, size=size, namespaces=namespaces,
                           profile=profile, config=config)
    if code == 0:
        print(f"[snapshot] wrote {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
