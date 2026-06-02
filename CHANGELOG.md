# Changelog

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
