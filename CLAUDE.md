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
  `Summary`, `Snapshot`) and unit helpers (`to_mcpu`, `to_mi`, `fmt_*`, `parse_timestamp`,
  `age_seconds`). `parse_timestamp` normalises RFC3339 fractions (Go emits 1-9 digits; Python
  < 3.11 accepts only 3 or 6) — every timestamp parse in the app must go through it. The
  single source of truth for shape. `to_mcpu`/`to_mi` clamp negative quantities to 0 (honoring
  the 'garbage yields 0' contract — Kubernetes never emits negatives). Carries **no** workload-specific
  knowledge (no namespace names, pod prefixes, or priorities).
- **`config.py`** — two structures: `Profile` (the *only* place workload literals live: pod
  ordering, timezone, alertmanager URL, health probes) and `Config` (runtime-editable: panels,
  columns, thresholds, namespaces, theme — the refresh cadence is fixed at 5s,
  `REFRESH_INTERVAL_SECS`, and no longer user-editable). Config resolution order (later wins):
  **defaults → profile file → `~/.config/kutop/config.yaml` → CLI overrides**. An unparseable
  user file never silently resets preferences: it is backed up to `config.yaml.invalid`
  (mode-preserving copy), the load falls back to defaults, and the failure travels in
  `Config.load_warnings` for the app to surface as a toast on mount. Configs/profiles
  resolve from `~/.config/kutop` and the packaged defaults only (legacy kubetop/ktop locations
  are no longer read or migrated). When a profile is active, `load_config(profile_authoritative=
  True)` lets the profile's namespaces/thresholds/probes win over the persisted file, and
  `save_config` does not persist those profile-owned fields — so a per-context profile (the
  `profiles_by_context` recall map) is not shadowed by, nor leaks into, the shared config file.
  Scalars are read as `x.get(k) or default` (a YAML key with no value is `None`, never the
  string "None"); `load_profile` coerces null/mis-typed sections and raises only `ValueError`;
  `save_config` writes the temp file `0600` and preserves an existing target's mode.
  Which fields count as "profile-owned" is defined once in `_profile_owned_sections()`
  (thresholds always; timezone/namespaces/context/probes only when the profile supplies them) —
  `_profile_layer`, `_strip_profile_owned`, and `_config_for_persist` all derive from it.
- **`fetch.py`** — `Fetcher` runs blocking `subprocess.run` kubectl calls. MUST run off the UI
  thread (the app drives it via a `@work(thread=True)` worker, pushes results with
  `call_from_thread`). Robustness contract: any failure sets `Snapshot.error` and returns a
  partial snapshot; callers keep the previous frame. Essential kubectl calls go through
  `_run_safe`, which records failures via `_record_fetch_error` (thread-safe, guarded by
  `_errors_lock`); `_fold_fetch_errors` merges those into `Snapshot.errors` as a **sorted**,
  deduplicated list — `Snapshot.error` is always `errors[0]` when fetcher-set, preserving the
  single-primary-failure contract. Calls whose failure is EXPECTED (metrics-server absent,
  kubelet stats, optional probes) use `_run_optional` instead: it diverts its failures into a
  **thread-local** scratch sink (never touches the shared list) that is simply discarded, so
  parallel workers' real failures are untouched and the refresh is not flagged. `_fetch_pods_for_namespace`
  now records unparseable JSON responses (exit 0 with malformed body) as `get pods -n <ns>: unparseable kubectl output`
  via `_record_fetch_error`, so silent pod-table drops are caught. `_as_int` helper parses per-volume byte counts in isolation,
  rejecting non-numeric and bool values, so a single malformed `usedBytes`/`capacityBytes` field no longer discards storage for every pod.
  PVC usage comes from the kubelet summary API (`/api/v1/nodes/<node>/proxy/stats/summary`) per node because
  metrics-server does not expose it — one node failing never aborts the refresh. `cancel()`
  kills in-flight processes for immediate quit. **Fan-out is bounded (issue #12):** a
  `BoundedSemaphore(_MAX_CONCURRENCY=4)` in `_run` caps simultaneous kubectl processes; `_node_summaries`
  caches each node's payload for `_NODE_SUMMARY_TTL=30s` keyed by `(context, node)`; and `enrich_snapshot`
  takes `heavy=` — a heavy cycle re-lists events/PVCs/probes and caches them, a light cycle re-attaches the
  cached lists (storage still re-fills from the TTL-cached summaries every cycle). The app runs the heavy
  cycle every `HEAVY_REFRESH_EVERY=3` ticks; a scope switch calls `fetcher.invalidate_caches()` and forces
  the next cycle heavy so a light cycle never shows another cluster's data. When
  watching `>= _ALL_NS_THRESHOLD=4` namespaces, `_fetch_pods/_fetch_events/_fetch_pvcs` issue ONE
  cluster-wide `-A` list (filtered client-side) instead of one call per namespace; a forbidden `-A`
  records the resource in `_all_ns_blocked` and falls back to the per-namespace fan-out (remembered
  for the session, cleared on scope switch). Pod usage is keyed by `(namespace, name)` so the same
  map serves the scoped `top` and the `top -A` paths. A `-A` list that **times out** falls back
  the same way as a forbidden one for that cycle (`_note_all_ns_failure`: forbidden pins the
  resource to scoped lists at once, `_ALL_NS_TIMEOUTS_TO_BLOCK=2` consecutive timeouts do), and
  on a large cluster (`total_namespaces >= _ALL_NS_FRACTION_MIN_TOTAL=40`, set by the app's
  discovery worker) `-A` additionally requires the watched set to be `_ALL_NS_MIN_FRACTION=15%`
  of it. `_node_summaries`
  is scoped to the nodes hosting a watched pod (`_nodes_to_summarize`; lossless because a
  kubelet reports its own pods' volumes only). `_parse_pod` aggregates regular + init +
  ephemeral container statuses via `_scan_container_statuses` (init failures surface as
  `Init:<reason>` while the pod is not Running; `status.reason` such as Evicted is used when no
  container reason exists). `_run` reduces kubectl stderr through `clean_kubectl_error` (klog
  prefixes stripped, kubectl's own summary line kept) before it becomes a `Snapshot.errors`
  entry. Failures are also logged to the `kutop.fetch` logger (opt-in file via `--log-file` /
  `KUTOP_LOG_FILE`, wired in `cli._setup_log_file`). Heavy-cycle caches carry a `_cache_scope` tag and a light cycle
  re-attaches them only when the tag matches the live scope. `kubectl top pods --containers`
  is retried without the flag only when the server rejects it (`_top_pods`). All subprocess
  output is decoded UTF-8 with `errors="replace"`.
- **`render/`** — split along class seams. **`app.py`** (`TopApp`) owns keybindings
  (`_BINDING_SPECS` is the single source of truth for keys), sorting, filtering, grouping,
  Options-modal wiring, and the pod actions (logs `l`, describe `d`, YAML `y`, shell `t`, delete `x`,
  rollout restart `X`). `modals.py` holds the log/describe/event modals and `YamlViewModal`
  (opens with `y`, streams `kubectl get pod <name> -n <ns> -o yaml`); `sidebar.py` the
  `SidebarPanel`/`SidebarState` (namespace checkboxes, sort/panel toggles, PROFILE/CONTEXT
  dropdowns, the 'Allow delete/restart (x/X)' gate, and the MENU section — Options/Keys/
  Screenshot/Quit — that replaced the old hamburger ThemeMenuModal: the ☰ header icon reveals
  the sidebar or focuses its first MENU button, and Esc inside the sidebar returns focus to the
  pod table); `options.py` the `OptionsModal` with a guided in-app health-probe editor on the Profile tab
  (add/remove probes with name, URL, optional label+regex field; live apply + Esc/Cancel revert); `table.py` the drag-resizable main table;
  `header.py` the header widgets; `widgets.py` the remaining presentation widgets (SummaryBar,
  TrendGraph, ConfirmModal, …); `render/theme.tcss` the styles. Pod-name filter (`/` search
  and `--filter` flag) detects regex metacharacters and compiles if valid at render time; invalid
  patterns or catastrophic-backtracking shapes (nested unbounded quantifiers or over 200 chars)
  fall back to substring match so untrusted/typo input can never hang the render thread. Filter
  matcher cached per term (`_filter_cache`) to avoid recompile on every render. First-run hint
  fires once on the first live snapshot of a default config (gated on `gen is not None` to keep
  synthetic/test frames toast-free). Empty-state messages are context-aware: active search shows
  `no pods match "<term>" — esc to clear`; empty scope names watched namespaces and filters.
  Startup guidance rows include the kube context name (`cluster unreachable (context: <ctx>): <error>`).
  Quit is keyboard-complete and two-step: `q` arms a 4s hint, a second `q` or Enter confirms,
  Esc cancels — `check_action` disables the Enter confirm while a modal, a text input, or the
  sidebar has focus. Delete and rollout restart are double-gated: the live 'Allow delete/restart'
  toggle (seeded by `--allow-destructive`, intentionally never persisted) plus a ConfirmModal
  naming the full target identity (context, namespace, pod, rollout target). Restart maps a
  ReplicaSet owner to its Deployment only when the RS name suffix is pod-template-hash-like
  (`model.pod_template_hash_like`); other ReplicaSets are reported un-rollable. While the
  cluster is unreachable before the first snapshot, the main table shows persistent guidance
  rows (error + `kubectl get nodes` hint + retry cadence) instead of a bare loading row; no
  toast fires before the first frame (the rows carry the detail), and a `localhost:8080`
  failure with no context resolved renders as `no kube context selected` guidance
  (`_looks_like_no_context`). Sidebar `Select` picks are never gated on `_syncing` —
  programmatic writes are suppressed with `prevent()`, so a `Changed` that arrives is the
  user's.
  Refresh errors are surfaced via `_notify_refresh_error` (deduped per severity+text);
  `_refresh_error_detail` aggregates `Snapshot.errors` into one toast line: up to 3 sources
  shown, each capped at 60 chars (`…`), with `+N more` beyond. `OptionsModal`
  (`render/options.py`): Esc / Cancel re-adopts the opening config snapshot via `apply_config`
  — persisting only when at least one edit was committed (config equality check short-circuits
  to a no-op dismiss otherwise); Close / `o` keeps changes. `apply_config` calls
  `_adopt_config(persist=True)` which saves via `save_config(profile=self.profile)` so
  profile-owned fields are correctly stripped. Fetch lifecycle: `_fetch_gen` is a scope
  token bumped on every namespace/context/profile switch so an in-flight old-scope result is
  dropped; scope changes/manual refresh go through `_request_refresh()` (queued if a fetch is
  in flight), timer ticks through `refresh_snapshot()` (skipped if in flight); `_force_heavy`
  is read-and-cleared under `_cadence_lock`. When the pod list fails outright (`errors` carry a
  `pods:`/`get pods` entry) but nodes succeed, the previous pods are carried over (summary
  counters recounted via `_recount_pod_summary`) with a distinct toast; while pods are carried
  or after a full failure the main panel title becomes `PODS · stale Ns` (`_last_good_refresh`
  only advances on a genuinely good frame). The CLI
  passes `reload_overrides` (base/CLI layers + `profile_authoritative`) so `R` re-runs the
  identical layering; `set_profile` takes timezone/alertmanager/probes from the profile only
  when it supplies them, else from the profile-free user layer. The kube context name is
  resolved on the discovery worker (never on the UI thread at mount). Namespace
  discovery has its own scope token (`_discover_gen`, bumped by
  `_start_ns_discovery`) so a listing from a superseded context is dropped; the
  sidebar NAMESPACES header shows `loading…`/`unavailable` via
  `SidebarPanel.set_ns_status`, and a failed listing is retried on the next
  good frame. Event rows use
  content-derived keys resolved through `_event_rows`. Modal headers, the event detail modal,
  the confirm modal body and the health-probe option list render cluster/user text as
  `rich.text.Text`, never markup. Textual PRIVATE
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
  (skippable via `--no-metrics-bootstrap` / `KUTOP_NO_METRICS_BOOTSTRAP=1`). A stderr notice
  announces the preflight (it can block ~12s before the TUI appears); the install prompt names
  the exact kubectl context it would mutate, and only an exact `y`/`yes` answer runs the
  cluster-mutating `kubectl apply`.
- **`probes.py`** — fetches `/`-prefixed profile URLs via `kubectl get --raw` through the
  API-server proxy (no port-forward), reusing kubeconfig auth. Direct URLs must be `http(s)`
  (any other scheme yields no data) and bodies are capped at 4 MiB. Field regexes are
  compiled through `regexsafe.safe_compile` (a rejected pattern skips just that field).
- **`regexsafe.py`** — the single backtracking screen (`safe_compile`, `has_nested_quantifier`,
  `looks_like_regex`) shared by the pod-name filter in `render/app.py` and probe field
  regexes. Any new user/profile-supplied regex must go through it.
- **`snapshot.py`** returns a `SnapshotResult` (an `int` exit code carrying `synthetic` and
  `error`) and keeps a partially-failed live frame; only an exception or a frame with no
  data at all falls back to the synthetic cluster, and the CLI says so when it does.
- **`cli.py`** — argparse entrypoint (textual stays lazily imported; see invariants). Validates
  `--sort`/`--summary-style` via argparse choices against the public `SORTABLE_KEYS`/
  `SUMMARY_STYLES` tuples; accepts-but-ignores the deprecated positional interval (stderr note
  plus an in-app toast; cadence is fixed at 5s); and fails fast with exit code 2 and setup
  guidance when kubectl is missing on the live path (`--self-test`/`--snapshot`/`--dump-config`
  still work without kubectl).

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
- **Pinned deps:** `textual==8.2.8`, `rich==15.0.0`; dev extras pin `ruff==0.16.5` and the
  lint rule set is explicit in `[tool.ruff.lint]` (`E`, `F`, `W`, `B`). Targets Python
  **3.9+** (CI matrix is 3.9 and 3.12) — avoid 3.10+-only syntax in runtime code. See `tests/conftest.py` for the 3.9
  event-loop shim that keeps synchronous Textual widget tests portable.
- **`storage_used_mi`/`used_mi` use `None` to mean "unknown"** (rendered `-`), distinct from a
  known `0`. Preserve that distinction.
