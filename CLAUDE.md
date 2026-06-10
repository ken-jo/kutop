# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kutop` is a btop-like **Kubernetes TUI dashboard** built on Textual/Rich. It is a
local terminal app with **no in-cluster agent** — every live read shells out to the
user's `kubectl` + kubeconfig exactly as `kubectl` would. PyPI/package/import name is
`kutop`; `kubetop` is a compatibility alias only.

## Commands

```bash
python -m pip install -e ".[profiles,test]"   # dev install
pytest -q                                     # full test suite
pytest tests/test_cli_config.py::test_name    # single test
ruff check                                    # lint
python -m compileall -q kutop kubetop tools   # byte-compile check (CI gate)
```

Cluster-free smoke checks (no kubectl, what CI runs and what to use for manual QA):

```bash
kutop --self-test           # headless render of one synthetic frame, asserts, exits 0
kutop --dump-config         # print full annotated config skeleton as YAML
kutop --snapshot out.svg --detail wide   # render one frame to SVG (live data or synthetic fallback)
```

Releases (PyPI/Homebrew/apt) are driven by `docs/release.md` and `.github/workflows/release.yml`;
version lives in `pyproject.toml` and `kutop/__init__.py`.

## Architecture

One refresh cycle flows **`fetch.py` → `model.Snapshot` → `render/app.py`**. The data
layer is deliberately UI-free and workload-free; the renderer applies Profile-driven
presentation (ordering, timezone, thresholds).

- **`model.py`** — domain dataclasses (`Pod`, `Node`, `PVC`, `Event`, `Alert`, `HealthResult`,
  `Summary`, `Snapshot`) and unit helpers (`to_mcpu`, `to_mi`, `fmt_*`, `age_seconds`). The
  single source of truth for shape. Carries **no** workload-specific knowledge (no namespace
  names, pod prefixes, or priorities).
- **`config.py`** — two structures: `Profile` (the *only* place workload literals live: pod
  ordering, timezone, alertmanager URL, health probes) and `Config` (runtime-editable: panels,
  columns, thresholds, namespaces, interval, theme). Config resolution order (later wins):
  **defaults → profile file → `~/.config/kutop/config.yaml` → CLI overrides**. Configs/profiles
  resolve from `~/.config/kutop` and the packaged defaults only (legacy kubetop/ktop locations
  are no longer read or migrated). When a profile is active, `load_config(profile_authoritative=
  True)` lets the profile's namespaces/thresholds/probes win over the persisted file, and
  `save_config` does not persist those profile-owned fields — so a per-context profile (the
  `profiles_by_context` recall map) is not shadowed by, nor leaks into, the shared config file.
  Which fields count as "profile-owned" is defined once in `_profile_owned_sections()`
  (thresholds always; timezone/namespaces/context/probes only when the profile supplies them) —
  `_profile_layer`, `_strip_profile_owned`, and `_config_for_persist` all derive from it.
- **`fetch.py`** — `Fetcher` runs blocking `subprocess.run` kubectl calls. MUST run off the UI
  thread (the app drives it via a `@work(thread=True)` worker, pushes results with
  `call_from_thread`). Robustness contract: any failure sets `Snapshot.error` and returns a
  partial snapshot; callers keep the previous frame. Essential kubectl calls go through
  `_run_safe` (failure recorded → `Snapshot.error`); calls whose failure is EXPECTED
  (metrics-server absent, kubelet stats, optional probes) use `_run_optional` instead so they
  never flag the refresh. PVC usage comes from the kubelet summary
  API (`/api/v1/nodes/<node>/proxy/stats/summary`) per node because metrics-server does not
  expose it — one node failing never aborts the refresh. `cancel()` kills in-flight processes
  for immediate quit.
- **`render/app.py`** (`TopApp`) — the Textual app; `render/widgets.py` holds custom widgets;
  `render/theme.tcss` + `theme.tcss` hold styles. Owns keybindings, the Options modal, sorting,
  filtering, grouping, log/describe/delete actions. Fetch lifecycle: `_fetch_gen` is a scope
  token bumped on every namespace/context/profile switch so an in-flight old-scope result is
  dropped; scope changes/manual refresh go through `_request_refresh()` (queued if a fetch is
  in flight), timer ticks through `refresh_snapshot()` (skipped if in flight). Textual PRIVATE
  imports live only in `render/_compat.py` — never import `textual.widgets._*` elsewhere.
- **`plugins/`** — optional, domain-specific features behind a tiny duck-typed seam
  (`KutopPlugin` Protocol: `panel_id`, `is_enabled(config)`, `fetch(fetcher, snapshot)`,
  `make_panel()`, `render(panel, snapshot)`). Registry is import-guarded; a missing/broken
  plugin is silently skipped and a plugin must **never raise** into the core. Add one by
  appending its module path to `_BUILTIN_PLUGIN_MODULES` in `plugins/__init__.py`. `health.py`
  is the reference plugin.
- **`snapshot.py`** — headless one-frame SVG renderer behind `--snapshot`; reused by
  `tools/snapshot.py` so there is a single render path. Falls back to a synthetic frame when no
  cluster is reachable.
- **`metrics.py`** — interactive Metrics Server preflight/bootstrap on live startup
  (skippable via `--no-metrics-bootstrap` / `KUTOP_NO_METRICS_BOOTSTRAP=1`).
- **`probes.py`** — fetches `/`-prefixed profile URLs via `kubectl get --raw` through the
  API-server proxy (no port-forward), reusing kubeconfig auth.

## Invariants to preserve

- **Workload-agnostic core.** `model.py`, `config.py`, `fetch.py`, and the renderer must stay
  free of workload literals (namespace names, pod prefixes, priorities). Such data enters only
  through a `Profile` YAML or live cluster reads. The `--self-test`/`--snapshot` synthetic
  frames use only generic names.
- **No textual import in the data/config layer.** `model.py`, `config.py`, `cli.py`, `fetch.py`
  keep textual lazily imported (in `cli.main`) so `--version`/`--help`/`--dump-config`/
  `--self-test` stay cheap and cluster-free.
- **Network/kubectl only when asked.** Empty alertmanager/health-probe config means those paths
  never touch the network — this is what keeps `--self-test` kubectl- and network-free.
- **Pinned deps:** `textual==8.2.7`, `rich==15.0.0`. Targets Python **3.9+** (CI matrix is 3.9
  and 3.12) — avoid 3.10+-only syntax in runtime code. See `tests/conftest.py` for the 3.9
  event-loop shim that keeps synchronous Textual widget tests portable.
- **`storage_used_mi`/`used_mi` use `None` to mean "unknown"** (rendered `-`), distinct from a
  known `0`. Preserve that distinction.
