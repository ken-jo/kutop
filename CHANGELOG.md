# Changelog

## Unreleased

### Fixed

- **Crash when a kubectl call timed out** (`MarkupError: Expected markup value`).
  A timeout surfaced `subprocess.TimeoutExpired`, whose text embeds the full
  argv — e.g. `Command '[... '--sort-by=.lastTimestamp', '-o', 'json']' timed
  out` — and the `[` (with the `=` inside) was parsed as Textual markup,
  crashing the whole app during layout. Toasts now render as plain text
  (`TopApp.notify` defaults `markup=False`), so no notification can be broken by
  its own dynamic content; the timeout message is also shortened to
  `timed out after 6s` instead of dumping the command line.

## 0.5.0 - 2026-06-11

### Added

- **Inspect pod YAML manifest** (`y`): a full-screen viewer that streams
  `kubectl get pod <name> -n <ns> -o yaml` for the focused pod (honoring the
  active --context), closeable with Esc or q.
- **Pod name filter now accepts regular expressions**: the `/` search and
  `--filter` flag understand both plain substring (case-insensitive) and regex
  patterns (detected by metacharacters and compiled lazily at render time); an
  invalid regex pattern or a catastrophic-backtracking pattern (nested unbounded
  quantifiers like `(a+)+` or over 200 chars) falls back gracefully to substring
  matching. The sidebar SEARCH hint shows "(regex)" when the active term is
  treated as a pattern.
- **Health-probe editor in Options modal**: the Profile tab now hosts a guided
  add/remove editor for health probes — give each probe a name, URL (http, https,
  or an API-proxy path starting with `/`), and an optional label + regex field;
  live apply with Esc/Cancel revert, so probes are no longer YAML-only.
- **First-run orientation toast**: a one-time welcome message on the first live
  snapshot of a fresh launch with the default profile and namespace (no health
  probes, alertmanager URL, or active filters); suppressed when a non-generic
  profile, custom namespaces, probes, or a live search/only_problems filter are
  active or on later runs.
- **Shell into the focused pod** (`t`): the dashboard suspends, hands the real
  terminal to `kubectl exec -it` (bash with sh fallback), and resumes with an
  immediate refetch when the shell exits.
- **Crashloop forensics in the log viewer**: `p` toggles `--previous` (the
  crashed container's logs — where the actual crash is, since a
  CrashLoopBackOff pod's live stream is empty or seconds-young), `c` cycles
  the target container on multi-container pods, and the header shows the last
  termination reason + exit code. The opt-in LAST REASON column now appends
  the exit code, e.g. `OOMKilled(137)`.
- The sidebar KEYS panel now covers every focus context: the dashboard shows a
  curated core set (`s` sort, `g` group, `/` search, `b` sidebar) instead of a
  placeholder, and focusing the warning-events table switches to an EVENTS
  context (`enter` details, `e` hide) the moment focus moves.
- **Rollout-restart action on `X`**: restarts the focused pod's controller
  (`deployment/<name>` derived from the ReplicaSet owner by stripping the
  trailing `-<pod-template-hash>`, or `statefulset/`/`daemonset/` directly)
  behind the same in-app **Allow delete** gate, with a full-identity confirm
  modal showing context, namespace, pod, and the exact `restarts:` target; bare
  pods and Job/unknown-owned pods get a warning toast suggesting delete (`x`)
  instead. The sidebar KEYS · POD ROW panel shows a Restart row that flips
  between "Restart" and "Restart disabled" with the destructive gate (sidebar toggle labelled "Allow delete/restart (x/X)").
- **Sidebar MENU section** hosting the former hamburger actions: Options, Keys,
  Screenshot, and Quit; menu Quit exits directly without the two-press `q`
  confirmation.

### Fixed

- **Main-table empty state is now actionable**: when a search filter is active,
  it names the term and how to clear it (`no pods match "<search>" — esc to clear`);
  when the scope is empty, it lists the watched namespaces and active filters
  (`no pods in [ns-a, ns-b] (hide_completed on) — b to change namespaces`).
- **Cluster-unreachable startup guidance now names the kube context** on its
  first row (`cluster unreachable (context: <name>): <error>`) so beginners can
  tell "right cluster, unreachable" from "wrong context".
- Fixed: a per-namespace `kubectl get pods` returning exit 0 with a truncated/garbage
  JSON body no longer silently wipes the pod table — the parse failure is now
  recorded (`get pods -n <ns>: unparseable kubectl output`) and surfaced as a
  refresh error, so the previous good frame is kept.
- Fixed: a single malformed `usedBytes`/`capacityBytes` field in a kubelet
  stats summary no longer discards PVC-backed storage for every pod in that
  refresh cycle — each volume entry is parsed in isolation, and a non-numeric
  byte field skips only that entry (a parse failure stays "unknown", never 0).
- Fixed: `to_mcpu`/`to_mi` now clamp negative quantities to 0, honoring their
  documented 'garbage yields 0' contract (Kubernetes never emits negative resource
  amounts).
- A malformed `~/.config/kutop/config.yaml` no longer silently resets every
  saved preference: the broken file is backed up to `config.yaml.invalid` before
  the app's next save can overwrite it, the app launches with defaults, and a
  warning toast explains what happened.
- While the very first cluster snapshot keeps failing (bad kubeconfig, VPN down),
  the pod table now shows persistent guidance rows — `cluster unreachable: …`,
  `check: kubectl get nodes`, and `retrying every 5s...` — instead of an eternal
  bare Loading row with only a 4s toast; the rows track error-text changes and
  the first successful snapshot clears them.
- An unreachable cluster now keeps the previous frame and shows an error toast
  (once per distinct error) instead of silently replacing the dashboard with an
  empty, error-free snapshot; partial failures (e.g. one forbidden namespace)
  apply what was fetched and surface a "refresh degraded" warning.
- PVC usage from the kubelet summary is keyed by **(namespace, name)** — two
  same-named claims in different namespaces no longer steal each other's
  usedBytes.
- Switching namespace/context/profile now queues an immediate refetch and
  discards any in-flight result fetched under the old scope, so the old
  cluster's data can no longer render under the new cluster's name.
- Modal close keys no longer leak into app bindings: closing logs/describe/event
  popups with `q` no longer arms the quit hint, and `Esc` no longer clears the
  active search filter.
- The saved `~/.config/kutop/config.yaml` can no longer be corrupted by values
  containing quotes/colons (e.g. a health-probe regex) — all user values are
  emitted as escaped YAML scalars, and the file is written atomically.
- A profile no longer erases user settings it does not supply: timezone,
  namespaces, context, and probes are only stripped/reset when the active
  profile actually carries them (thresholds remain always profile-owned).
- `kutop --profile <typo-or-broken-yaml>` prints one clean error line instead
  of a traceback.
- Memory/CPU quantity parsing handles exponent forms (`1e9`), the SI `k`
  suffix, and nano/micro CPU suffixes instead of silently reading them as 0.
- SummaryBar tiles no longer wrap (and lose the value row) on narrow
  terminals — whole tiles are dropped to fit; threshold sliders map clicks to
  the exact cell under the cursor and show a combined marker when warn/crit
  overlap; trend meters use the real content width.
- The sidebar CONTEXT dropdown is only rebuilt when its options/value actually
  change, so it no longer snaps shut on every 5s refresh; Options-modal edits
  no longer rebuild the pod table (losing cursor/scroll) when columns are
  unchanged.
- Opening Options no longer shells kubectl on the UI thread; a timed-out
  kubectl is drained with a bounded wait so a credential-plugin child can no
  longer hang the fetch worker; a process spawned during `cancel()` is now
  killed too.
- The PVC panel's last column is now labelled **CLASS** (it shows the storage
  class); the dead `Tab` sidebar binding was removed (`b` toggles the sidebar).
- Changing sort or columns before the first snapshot no longer blanks the
  table: the loading row / startup-guidance rows are re-rendered after every
  column rebuild.
- A partially failing refresh now reports every broken source instead of only
  the first: the "refresh degraded" toast aggregates up to 3 distinct failures
  (e.g. `refresh degraded: 2 failures: get pods -n team-a: forbidden; get pvc
  -n team-b: timeout`), each capped at 60 chars (`…`), with `+N more` beyond,
  making multi-namespace RBAC problems diagnosable at a glance.
  Namespace-scoped kubectl failure labels include the namespace so identical
  errors in different namespaces no longer collapse into one message.
  `Snapshot.errors` now carries every distinct failure of the cycle;
  `Snapshot.error` retains its existing single-primary-failure contract for
  backward compatibility.
- Delete/restart failure toasts no longer truncate kubectl stderr to 80 chars —
  the reason is shown whitespace-collapsed up to 200 chars, and the complete
  stderr is written to the Textual devtools log.

### Changed

- Options modal: `Esc` and a new **Cancel (esc)** footer button now revert
  every edit made while the modal was open. When at least one edit was
  committed, `apply_config` re-adopts the opening snapshot and persists it;
  if nothing was committed (or only a theme was previewed without applying),
  the modal dismisses without touching the config file. The **Close** button
  and the `o` key keep changes, as before.
- The two-step `q` quit flow is now keyboard-complete: `Enter` confirms a
  pending quit and `Esc` cancels it without also clearing an active search in
  the same keypress. The confirm only fires on the base dashboard — opening a
  modal or the search bar abandons the pending quit, and `Enter` otherwise
  reaches the focused widget unchanged.
- The pod delete confirmation spells out the full target identity — cluster
  context, namespace, and pod name — before anything executes.
- **Fail fast on missing `kubectl`**: exit code 2 with an actionable stderr
  message (install kubectl, set KUBECONFIG, verify with `kubectl get nodes`)
  instead of launching a TUI that can never load; `--self-test`, `--snapshot`,
  and `--dump-config` still run kubectl-free.
- The header hamburger (☰) now opens the unified sidebar command surface: it
  reveals the sidebar when hidden and focuses the sidebar's MENU section when
  already visible — no longer opens a popup menu.
- `Esc` inside the sidebar returns focus to the pod table.
- **`--sort` and `--summary-style`** now validate their values at the command
  line (rejecting unknown values with a clear argparse error) instead of
  silently coercing them to defaults; `--sort` help now lists the full
  canonical key set (`priority`/`name`/`cpu`/`mem`/`cpu_pct`/`mem_pct`/`restarts`/`phase`/`node`/`namespace`/`age`/`storage`/`owner`).
- An unknown `--theme` (or a stale theme name in the config file) still
  launches with the default theme but now shows a warning toast instead of
  falling back silently.
- The metrics-server preflight now announces itself on stderr before its
  `kubectl` calls (`[kutop] checking metrics-server (up to ~12s; skip with
  --no-metrics-bootstrap)…`) instead of appearing to hang for up to ~12s
  before the TUI shows.
- The interactive Metrics Server install prompt now names the `kubectl` context
  the apply would target (e.g. `[kutop] Install Metrics Server into context
  'dev' via the official components manifest now? [y/N]`) and advertises No as
  the default; an empty answer still declines and only an explicit `y` or `yes`
  applies the manifest.
- Passing the deprecated positional interval argument (e.g. `kutop ns 5`) now
  also shows a one-time in-app toast after the TUI mounts in addition to the
  stderr notice, which the fullscreen TUI previously covered immediately.
- Removed the ThemeMenuModal popup and `TopApp.action_open_theme_menu`
  (replaced by the sidebar MENU; `kutop.render.options` no longer defines
  `ThemeMenuModal` and `kutop.render.widgets` no longer re-exports it).
- `kutop --help` now ends with a short orientation epilog: prerequisites,
  the config-file path with a `--dump-config` pointer, the profiles
  directory, and the docs URL.
- README: beginner-first restructure (Quick start, a no-cluster demo path,
  Configuration, Troubleshooting, FAQ), and the "Latest release" badge is now
  PyPI-backed — immune to the shields.io GitHub token-pool exhaustion that
  intermittently rendered "Unable to select next GitHub token from pool".
- CI now enforces `ruff check` and runs a non-blocking latest-textual/rich
  canary job; Textual private-API imports are confined to
  `kutop/render/_compat.py`.
- README options screenshots are shown one per tab at full width; `llms.txt`
  added for LLM-assisted tooling.
- Standalone or CRD-managed ReplicaSets (e.g. `web-canary`, Argo Rollouts) are
  no longer misreported as a Deployment owner: the `<deploy>-<podTemplateHash>`
  strip only happens when the suffix actually looks like a pod-template-hash,
  and `X` refuses such pods instead of restarting an unrelated same-prefix
  Deployment.
- Docs: synced `llms.txt` and `CLAUDE.md` with the wave-1 feature set (render/
  module split, gated rollout-restart `X`, sidebar MENU, two-step quit,
  kubectl fail-fast, persistent unreachable-cluster guidance, config.yaml.invalid
  backup, CLI validation, and the metrics prompt); `docs/release.md` now names
  both version locations (`pyproject.toml` and `kutop/__init__.py`) so
  `kutop --version` cannot drift from the published release.

### Tests

- Exhaustive Config persistence round-trip that walks every dataclass field, so
  a new option missing from the dump/load mapping can never ship silently;
  CLI-flag coverage for every previously untested flag and positional; `kubetop`
  compatibility alias regression-tested (import, version parity, legacy
  submodule imports, `python -m kubetop --version`).

## 0.4.0 - 2026-06-09

### Added

- Sidebar **PROFILE** dropdown to switch the active workload profile live; a
  profile may set a `context:` so selecting it also switches to that kube
  context (cluster). The last active profile is remembered across launches, and
  an opt-in **"Remember for this context"** toggle records a per-context profile
  (`profiles_by_context`) in `~/.config/kutop/config.yaml` — never in kubeconfig.
- Sidebar **CONTEXT** dropdown to switch the kube context (cluster) on its own,
  discovered from your kubeconfig; selecting one rewires the fetcher,
  re-discovers that cluster's namespaces, and refetches immediately.
- In-app **"Allow delete"** sidebar toggle that gates pod deletion (select a pod
  row, press `x`, confirm in a popup). `--allow-destructive` now only seeds this
  toggle's initial state.
- A fixed `metrics 15s` header readout showing metrics-server's scrape
  resolution (how fresh the CPU/MEM numbers are).

### Changed

- The refresh cadence is now **fixed at 5s** and no longer adjustable; the
  top-right interval adjuster and the Options-modal interval stepper were
  removed. Polling faster than metrics-server's scrape resolution only re-shows
  identical metric values, so the knob was misleading.
- A legacy second positional (`kutop <ns> <interval>`) is still accepted but
  ignored with a one-line notice.
- Removed the ktop/kubetop legacy config/state migration and legacy profile
  directories; configs and profiles now resolve from `~/.config/kutop` and the
  packaged defaults only. PyYAML is a hard runtime dependency.

### Fixed

- The CONTEXT dropdown could freeze the app on a cluster switch (an unbounded
  `set_context` ↔ `Select.set_options` feedback loop) and then crash with
  `NoMatches`; the dropdown now refreshes without re-entering and `_adopt_config`
  guards its widget lookups.
- Persistence no longer lets the saved config shadow the active profile or leak
  one context's profile into others: profile-owned values (namespaces,
  thresholds, timezone, probes, context) are re-supplied by the profile layer
  while the active profile name is retained, so a profile's custom health
  survives a restart. Saves now honour `--config`.
- Summary tiles colour CPU/MEM from the active thresholds; fixed three ruff
  F821 forward-reference warnings and de-duplicated the health-probe coercion.

## 0.3.3 - 2026-06-04

### Added

- Added a context-sensitive Keys sidebar panel that stays fixed at the bottom
  of the sidebar and shows the active interaction context, such as pod row
  actions (`Logs`, `Describe`, `Delete`) or search-mode actions.
- The header now shows the running version next to the app title
  (`kutop v0.3.3`).

### Changed

- The sidebar uses clearer section spacing while keeping its controls in a
  scrollable region above the fixed Keys panel.
- The refresh cadence readout now keeps the smaller refresh glyph directly next
  to the interval value (`- ↻ 2.6s +`) instead of floating it separately.
- Pressing `q` now follows a two-step TUI quit flow: the first press shows a
  confirmation toast and the second press within the toast window exits.

## 0.3.2 - 2026-06-04

### Added

- Live startup now checks `kubectl top nodes` and the `metrics.k8s.io`
  discovery endpoint before entering the TUI. If Metrics Server appears absent,
  interactive terminals ask `[y/n]`; `y`, `Y`, `yes`, `YES`, and other
  `y`-prefixed answers apply the official Metrics Server components manifest,
  while `n`/empty/other answers leave the cluster unchanged and print the
  components and Helm install options for review.
- Added `--no-metrics-bootstrap` and `KUTOP_NO_METRICS_BOOTSTRAP=1` to skip the
  startup Metrics Server prompt in scripted runs.

### Fixed

- CPU/MEM trend history now accepts a real 0% sample when capacity is known,
  instead of carrying a stale previous value forever. Namespace/context changes
  also reset trend history so old cluster scope data does not bleed into the new
  view.

## 0.3.1 - 2026-06-03

### Fixed

- Launching no longer overwrites `~/.config/kutop/config.yaml`. `on_mount`
  ended in a `_persist_state()` that rewrote the loaded config on every start,
  so a launch where the load silently fell back to defaults — e.g. PyYAML
  unavailable (it was an optional extra before 0.2.2), or an unrecognized
  legacy key — reset the user's real settings (profile, probes, view options).
  Persistence is now opt-in for genuine user actions (panel toggles, Options
  apply) only; render-time callers never touch the file.

### Changed

- The sidebar fits short terminals: the status block is a single rule instead
  of a bordered box-in-box, the `filter=` line shows only when a filter is
  active, and tighter spacing keeps the SORT / PANELS controls reachable.
- The sidebar shows the real kube context name (resolved via
  `kubectl config current-context`) instead of a generic "current"; long EKS
  ARNs show the trailing cluster segment (`…/cluster/spm-eks` → `spm-eks`).
- Alerts and Health panels that are toggled on but unconfigured now render a
  setup hint (`set probes.alertmanager_url to enable`, `no probes configured`)
  instead of silently showing nothing.

## 0.3.0 - 2026-06-03

### Added

- Interactive refresh-cadence control. The header shows the current interval as
  `⟳ - <value> +` at the top right (to the left of the clock, btop-style), and
  `+`/`-` (also `=`) adjust it live in 100 ms steps over 1.0–60.0s. The Options
  "View" field is now a matching `- <value> +` stepper instead of a free-text
  input, and both surfaces stay in sync through a single `clamp_interval` source
  of truth.

### Changed

- The CPU/MEM trend panels share one continuous color ramp across the heat strip
  and the `now` gauge bar for a btop-style heatmap: the heat strip fills below
  the curve with vertical braille blocks colored per column along the ramp
  (blank above the curve), and the gauge bar runs the same gradient along its
  length instead of a single threshold color.

### Tests

- Added headless coverage for the heat strip rendering, the interval indicator
  placement/value tracking, the `+`/`-` clamp behavior, and the Options
  stepper → app → header sync.

## 0.2.2 - 2026-06-01

### Changed

- Promote PyYAML from the optional `profiles` extra to a default runtime
  dependency so `uvx kutop@latest` can read YAML config and profile files without
  extra install syntax.

## 0.2.1 - 2026-06-01

### Fixed

- Keep the sidebar `SORT` and `PANELS` checkbox labels visible in the dark TUI.
  Textual 8 renders default checkboxes with tall chrome, while kutop's sidebar
  intentionally lays these controls out as one-row items. The sidebar now uses
  compact checkboxes so labels such as `Descending`, `Summary`, `Alerts`, and
  `Health` are rendered instead of being clipped to faint empty boxes.

### Tests

- Added a headless SVG snapshot regression test that asserts the main sidebar
  checkbox labels are present in the rendered output.
- Verified the release package with unit tests, compile checks, CLI smoke tests,
  self-test rendering, `python -m build`, and `twine check`.

## 0.2.0 - 2026-06-01

### Added

- Initial PyPI/GitHub release of `kutop`, a btop-like Kubernetes TUI dashboard
  for pods, nodes, CPU, memory, events, PVC usage, alerts, and health checks.
