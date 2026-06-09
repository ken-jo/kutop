# Changelog

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
