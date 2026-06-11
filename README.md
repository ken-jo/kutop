# kutop

[![Latest release](https://img.shields.io/pypi/v/kutop?label=latest%20release&logo=pypi&logoColor=white&cacheSeconds=300)](https://github.com/ken-jo/kutop/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/ken-jo/kutop/ci.yml?branch=master&label=CI&logo=githubactions&logoColor=white&cacheSeconds=3600)](https://github.com/ken-jo/kutop/actions/workflows/ci.yml)
[![Release workflow](https://img.shields.io/github/actions/workflow/status/ken-jo/kutop/release.yml?label=release%20workflow&logo=githubactions&logoColor=white&cacheSeconds=3600)](https://github.com/ken-jo/kutop/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](pyproject.toml)
[![kubectl](https://img.shields.io/badge/kubectl-%C2%B11%20minor%20of%20cluster-326ce5?logo=kubernetes&logoColor=white)](https://kubernetes.io/releases/version-skew-policy/)
[![Metrics Server](https://img.shields.io/badge/metrics--server-0.6.x%2B%20%7C%20K8s%201.19%2B-326ce5?logo=kubernetes&logoColor=white)](https://kubernetes-sigs.github.io/metrics-server/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Issues](https://img.shields.io/github/issues/ken-jo/kutop?cacheSeconds=3600)](https://github.com/ken-jo/kutop/issues)
[![Stars](https://img.shields.io/github/stars/ken-jo/kutop?style=social&cacheSeconds=3600)](https://github.com/ken-jo/kutop/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/ken-jo/kutop?cacheSeconds=3600)](https://github.com/ken-jo/kutop/commits/master)

`kutop` is a modern, like-btop **Kubernetes TUI dashboard** for the terminal. It
turns `kubectl` and your kubeconfig into a fast, readable view of pods, nodes,
namespaces, CPU, memory, restarts, OOMKilled pods, warning events, PVC storage,
Alertmanager alerts, and custom health checks.

It is built with [Textual](https://textual.textualize.io/) and runs locally with
no in-cluster agent. `kutop` is useful when you want a Kubernetes terminal
dashboard, pod monitor, node resource view, k8s observability console, or a
`kubetop`/`ktop` style CLI that feels closer to `btop`.

![kutop Kubernetes TUI dashboard — sidebar profile/context switchers, a fixed 5s refresh with a metrics-server freshness readout, and Summary, Trends, Alerts, custom Health, Pods, Events, and PVC panels (demo data)](docs/kutop-main-all-panels.svg)

## Quick start

Three checks, one install command, one run command.

1. Check your tools. Each command should succeed:

   ```bash
   kubectl version --client   # kubectl is installed and on PATH
   kubectl get nodes          # kubeconfig and auth reach your cluster
   kubectl top nodes          # metrics-server serves CPU/MEM numbers
   ```

   If only the last one fails, keep going — kutop offers to install Metrics
   Server for you on first run. If the others fail, see
   [Troubleshooting](#troubleshooting).

2. Install kutop:

   ```bash
   python -m pip install kutop
   ```

3. Run it:

   ```bash
   kutop                # watch the 'default' namespace
   kutop my-namespace   # or name a namespace
   ```

**What you should see:** a header with your cluster context and a `metrics 15s`
freshness readout, a cluster summary row, and a pod table that fills within
about 5 seconds. Before the TUI appears, kutop prints a one-line stderr notice
while it probes for Metrics Server. If Metrics Server is missing, kutop asks
to install it, naming the kubectl context it would modify and defaulting to No
— only an explicit `y` applies anything to your cluster.

On your first launch with a default config, you'll see a brief orientation
toast naming the core keys. When you search for pods or select a namespace filter
that finds no results, the empty-state message tells you why and how to fix it
(e.g., `no pods match "xyz" — esc to clear` or `no pods in [team-a] — b to change
namespaces`). If the cluster is unreachable before the first snapshot, the guidance
rows show the kube context so you can verify you're looking at the right cluster.

**First keys:** press `q` twice to quit, `o` for options, `b` for the sidebar,
`/` to search pods. Full list under [Keybindings](#keybindings).

Something looks wrong? Jump to [Troubleshooting](#troubleshooting).

### No cluster yet?

Spin up a free local cluster:

```bash
# minikube
minikube start
minikube addons enable metrics-server

# or kind (metrics-server needs --kubelet-insecure-tls on kind)
kind create cluster
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

Or preview kutop with no cluster at all — both commands are kubectl-free and
render a synthetic demo frame:

```bash
kutop --self-test          # headless smoke test, prints OK and exits 0
kutop --snapshot demo.svg  # writes one demo dashboard frame to an SVG
```

## Highlights

* Kubernetes pod and node monitoring directly in the terminal.
* Live CPU and memory trend sparklines plus per-pod usage-vs-limit gauges.
* Problem-first signals for Pending, Failed, OOMKilled, CrashLoopBackOff, and
  restarting workloads.
* **Sidebar profile & context switchers** — pick a workload profile (it can
  bundle its own kube context) or hop clusters live; your last profile is
  remembered across launches.
* **Fixed 5s refresh** for problem signals, plus a `metrics 15s` header readout
  showing how fresh the CPU/MEM numbers are (metrics-server's scrape resolution).
* Optional Events, PVC storage, Alertmanager, health-check, and contextual Keys
  sidebar panels.
* Multi-namespace views, sorting, filtering, grouping, and a configurable
  sidebar for fast cluster triage.
* Profile-driven thresholds, pod ordering, timezone, alert sources, and health
  probes so the core stays generic.
* Confirm-gated pod deletion and workload rollout-restart via an in-app
  **Allow delete/restart (x/X)** toggle — no global launch flag required.
* Headless SVG screenshots for README assets, release notes, and visual QA.

```
NODES 2/2 │ PODS(R/P/F) 18/1/0 │ RESTARTS 7 │ OOM 1 │ WARN 2 │ ALERTS 3
CPU OVERALL  ▁▂▃▅▆▇█  62%  5.1/16        MEM OVERALL  ▃▄▅▆▇█  74%  47/64Gi
◆ worker-pool  node-a │
  ● api-0 (1/1)        ███████░░░ 70%   STS
  ● worker-9 OOMKilled (0/1)  █████████░ 95%   Deploy
```

## Install

One command:

```bash
python -m pip install kutop
```

Prefer isolated tool installs? `pipx install kutop` and `uvx kutop` work too.

Upgrade later with:

```bash
python -m pip install -U kutop
```

This installs the `kutop` command and `python -m kutop`. The `kubetop` command
and `python -m kubetop` ship alongside as compatibility aliases — see the
[FAQ](#faq) for the naming story.

Homebrew (the tap is updated automatically with each release):

```bash
brew install ken-jo/kutop/kutop   # one-shot tap + install
```

A signed apt repository is planned but not published yet; use pip or Homebrew
on Debian/Ubuntu for now.

### From source (development)

```bash
# latest master
python -m pip install "kutop @ git+https://github.com/ken-jo/kutop.git"

# a specific tag
python -m pip install "kutop @ git+https://github.com/ken-jo/kutop.git@v0.4.0"

# editable install from a clone
git clone https://github.com/ken-jo/kutop.git
cd kutop
python -m pip install -e ".[test,profiles]"
```

## Requirements & permissions

`kutop` is a local terminal app. It does not install an in-cluster agent; it
uses your local `kubectl` and kubeconfig exactly as `kubectl` would.

| Dependency | Required version / contract | Used for |
|------------|-----------------------------|----------|
| Python | 3.9+ | running the installed `kutop` package |
| Textual / Rich | `textual==8.2.7`, `rich==15.0.0` | terminal UI rendering |
| kubectl | installed on `PATH`; use a client within +/- 1 minor version of your cluster's kube-apiserver | all live cluster reads and actions |
| kubeconfig | current context from `~/.kube/config`, `KUBECONFIG`, or `--context` | cluster auth and target selection |
| Metrics Server | 0.6.x+ on Kubernetes 1.19+ recommended | `kubectl top` CPU/MEM metrics |

Live dashboard mode requires `kubectl` on PATH. If `kubectl` is missing, kutop
exits immediately with code 2 and prints step-by-step guidance (install
kubectl, set KUBECONFIG, verify with `kubectl get nodes`). `KUBECONFIG` is
honored exactly as `kubectl` honors it. `kutop --self-test`,
`kutop --snapshot`, and `kutop --dump-config` are kubectl-free and work
without a cluster.

CPU and memory values use `kubectl top nodes` and
`kubectl top pods --containers`, then sum container rows so multi-container pods
show pod-level usage. If container-level `top` is unavailable, `kutop` falls
back to pod-level `kubectl top pods`.

On live startup, kutop prints to stderr:

```
[kutop] checking metrics-server (up to ~12s; skip with --no-metrics-bootstrap)…
```

then checks `kubectl top nodes` plus the `metrics.k8s.io` discovery endpoint.
If Metrics Server appears absent and the terminal is interactive, kutop asks
before changing the cluster. The prompt names the kubectl context it would
modify and defaults to No:

```
[kutop] Install Metrics Server into context 'my-cluster' via the official
components manifest now? [y/N]
```

Only `y` or `yes` (case-insensitive) applies the manifest; any other answer, including an empty one, leaves the cluster unchanged.
Pressing `y` or `yes` runs the official components manifest:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

Answering `N` leaves the cluster unchanged and prints both the components
manifest path and the official Helm route for review. Use
`--no-metrics-bootstrap` or `KUTOP_NO_METRICS_BOOTSTRAP=1` to skip this startup
check and prompt entirely.

PVC storage usage is fetched through the Kubernetes API-server proxy with
`kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary`; this reuses the
same kubeconfig auth and does not need a localhost port-forward.

RBAC needs depend on which panels/actions you use:

* Core dashboard: `get/list` pods, nodes, events, and PVCs in the selected
  namespaces.
* CPU/MEM metrics: access to the Metrics API used by `kubectl top`.
* PVC usage and profile proxy URLs: permission for `get --raw` API-server proxy
  paths.
* Logs/describe/delete/restart: the corresponding pod `logs`, `get`, `delete`,
  or `kubectl rollout restart` permissions. Both `x` (delete) and `X` (rollout
  restart) stay disabled until you turn on the **Allow delete/restart (x/X)** toggle in the
  sidebar (or launch with `--allow-destructive`, which just seeds that toggle
  on) AND accept the confirmation popup. The toggle is not persisted — it
  resets to off on each launch.

## Run

Start with plain `kutop`; everything else is optional:

```bash
kutop                               # generic view, namespace 'default'
kutop demo-ns                       # namespace demo-ns
kutop ns-a,ns-b                     # multiple namespaces (comma list)
kutop --profile example             # load a profile (ordering / tz / thresholds)
kutop --filter '^web-'              # filter pod names by regex or substring
python -m kutop demo-ns             # module form
python -m kubetop demo-ns           # legacy module alias
kutop --context demo-context demo-ns  # pick a kubeconfig context
kutop --sort cpu                    # initial sort key: priority/name/cpu/mem/cpu_pct/mem_pct/restarts/phase/node/namespace/age/storage/owner (unknown values exit 2)
kutop --allow-destructive           # start with the 'Allow delete/restart (x/X)' toggle on — enables both x delete and X restart (still confirm-gated)
kutop --no-metrics-bootstrap        # skip startup Metrics Server prompt
kutop --dump-config                 # print the full annotated config skeleton
kutop --self-test                   # headless smoke test (no cluster), exits 0
kutop --snapshot out.svg            # render one frame to SVG and exit
kutop --snapshot out.svg --detail full  # wider diagnostic capture
```

The positional `namespaces` only seeds the first run; your in-app choices are
saved to `~/.config/kutop/config.yaml` and win on the next launch. The refresh
cadence is fixed at 5s and is no longer configurable — a legacy second
positional (`kutop demo-ns 3`) is still accepted but ignored. kutop reminds
you both on stderr and via a one-time in-app toast after launch that the
interval argument is deprecated and the refresh is fixed at 5s. CPU/MEM
metrics come from metrics-server, whose default scrape resolution is 15s, so
the header shows a fixed `metrics 15s` freshness readout (polling faster than
that would just re-show identical values).

## Keybindings

| Key | Action |
|-----|--------|
| `q` `q` | quit (first press shows a confirmation toast) |
| `r` | refresh now |
| `o` | options / settings (tabbed: View, Columns, Panels, Thresholds, Cluster, Profile); inside the modal, **Close** (or `o`) keeps changes while **Cancel (esc)** / `Esc` discards every edit and restores the settings as they were when the modal opened *(unreleased)* |
| `b` | toggle the control sidebar; the header ☰ button also reveals the sidebar or, if already visible, jumps focus to the sidebar MENU section (Options / Keys / Screenshot / Quit — menu Quit exits immediately, unlike the two-press `q` flow) *(unreleased)* |
| `/` | search / filter pods by name (accepts regular expressions; plain terms use case-insensitive substring match) |
| `Esc` | clear the search filter |
| `Esc` (in Options modal) | cancel / discard all edits and restore the pre-open settings *(unreleased)* |
| `s` / `S` | cycle sort column / flip sort direction (or click a column header) |
| `g` | group pods under their node |
| `l` | logs for the focused pod (`kubectl logs -f`) |
| `d` | describe the focused pod |
| `y` | YAML manifest for the focused pod (`kubectl get pod <name> -n <ns> -o yaml`) *(unreleased)* |
| `t` | shell into the focused pod (`kubectl exec -it`, bash with sh fallback) — the dashboard suspends and resumes when the shell exits *(unreleased)* |
| `x` | delete the focused pod (needs the sidebar **Allow delete/restart (x/X)** toggle on, then confirm) |
| `X` | restart the focused pod's workload via `kubectl rollout restart` (same **Allow delete/restart (x/X)** gate, then a confirm showing context / namespace / pod / target; the Deployment is derived from the ReplicaSet name) *(unreleased)* |
| `Esc` (in sidebar) | return focus to the pod table *(unreleased)* |
| `e` / `v` | toggle the Events / PVC panels |
| `a` / `h` | toggle the Alerts / Health panels (profile-driven) |
| `R` | reload `~/.config/kutop/config.yaml` live |
| `Tab` / `Shift+Tab` | move focus between widgets (terminal-standard, built into Textual) |

Inside the full-screen viewers (logs, describe, event details):

| Key | Action |
|-----|--------|
| `q` / `Esc` | close the viewer |
| `p` | log viewer: show the **previous (crashed) container's** logs *(unreleased)* |
| `c` | log viewer: cycle containers on multi-container pods *(unreleased)* |

Keys marked *(unreleased)* are on `master` but not yet in a tagged release —
they ship in the next release after 0.4.0.

In `o` → Thresholds, the sliders also work without a mouse: `←`/`h` and
`→`/`l` move the active handle, and `[`, `]`, or `Space` switch between the
warn and crit handles.

The **NODE/POD column is resizable**: drag the `│` handle on its header to widen
or narrow it (the width persists). Click any column header to sort by it.

The sidebar Keys panel intentionally shows only the current work context. For
example, a focused pod row surfaces `l` logs, `d` describe, `x` delete, `t` shell, and `X` restart, while
the search bar surfaces `/`, `Enter`, and `Esc`; global shortcut summaries stay
in the footer and native help.

## Configuration

All of kutop's saved state lives in one file:

```text
~/.config/kutop/config.yaml
```

Settings are layered; later layers win:

```text
built-in defaults -> --profile -> ~/.config/kutop/config.yaml -> CLI flags
```

You rarely need to edit the file by hand. Every change you make in the running
app — the `o` Options modal, sidebar toggles, column resizes — is written back
to it immediately (and atomically). If you do edit it by hand, press `R` in the
app to reload it live.

To see every available key with its default and an explanation, print the
built-in annotated reference:

```bash
kutop --dump-config
```

```yaml
# kutop configuration skeleton — every option, with defaults.
# Location: ~/.config/kutop/config.yaml  (edit by hand or via the
# Options modal, key 'o', in the running app). Layering order:
#   built-in defaults -> --profile -> this file -> CLI flags.
profile: "generic"        # active profile name (read-only)
view:
  timezone: ""          # IANA tz for timestamps; "" = host local
  theme: "textual-dark"    # app theme; choose from Options (o)
# ... every other option follows, each with an explanatory comment
```

Thresholds, alert sources, and health probes can also come from a profile (see
[Profiles](#profiles)); the panels/probes YAML shape is shown in
[Alerts & custom panels](#alerts--custom-panels-no-port-forward).

**If your config file is broken:** if `~/.config/kutop/config.yaml` cannot be
parsed, kutop launches with defaults, shows a warning toast, and saves a copy
of the broken file to `config.yaml.invalid` so your hand edits are never lost.
Fix the file (or restore from the `.invalid` backup) and press `R` in the app
to hot-reload it.

**Unknown theme:** if `--theme` names a theme that does not exist (or the
config file contains a stale theme name), kutop still launches with the default
theme and shows a warning toast instead of failing silently.

**`--sort` and `--summary-style` validation:** passing an unknown value exits
immediately with code 2 and lists the valid choices — the old silent fallback
to defaults is gone.

To start over, delete the file — kutop recreates it with defaults on the next
launch:

```bash
rm ~/.config/kutop/config.yaml
```

## Profiles

A profile externalises everything that would otherwise be hardcoded. Every
field is optional. See
[`kutop/profiles/example.yaml`](kutop/profiles/example.yaml) for the fully
commented template; a minimal profile looks like:

```yaml
name: my-stack
namespaces: [team-a, team-b]
ordering:                     # pin important workloads to the top
  - { prefix: ingress-, weight: 10 }
  - { prefix: api-,     weight: 20 }
thresholds:
  cpu_warn: 75
  cpu_crit: 90
```

To create your own, copy the template and pass its name:

```bash
mkdir -p ~/.config/kutop/profiles
curl -fsSL https://raw.githubusercontent.com/ken-jo/kutop/master/kutop/profiles/example.yaml \
  -o ~/.config/kutop/profiles/my-stack.yaml
kutop --profile my-stack
```

Profiles resolve by name from `~/.config/kutop/profiles/<name>.yaml` and the
packaged `kutop/profiles/` directory, or by explicit path. Without a profile
the core runs fully (alphabetical ordering, local timezone, generic thresholds).

The active profile can also be **switched live** from the **PROFILE** dropdown
at the top of the sidebar (`b`). The list is discovered from your profile
directories (plus `generic` for the no-profile default); selecting one re-applies
that profile's ordering, namespaces, timezone, thresholds, alert source, and
health probes immediately, and refetches at once. If the profile sets a
`context:`, selecting it also switches to that kube context (cluster) — so a
profile can bundle "which cluster + how to view it"; leave it empty to keep the
current context.

Below PROFILE, the sidebar's **CONTEXT** dropdown switches the active kube
context (cluster) on its own — for hopping between clusters without a profile.
Its list is discovered from your kubeconfig; selecting one rewires the fetcher,
re-discovers that cluster's namespaces, and refetches immediately. (`o` →
Cluster also has the same picker, and `--context` still works at launch.)

By default a live switch is session-only. Tick **Remember for this context**
(below the dropdown) to persist the choice **keyed by your current kube context**:
kutop stores a `context → profile` map (`profiles_by_context`) in
`~/.config/kutop/config.yaml` and, on the next launch without `--profile`,
auto-loads the profile remembered for the active context — so each cluster keeps
its own workload profile. An explicit `--profile` always wins and skips the
lookup. This is stored in kutop's own config only; kutop never writes to your
kubeconfig.

## Alerts & custom panels (no port-forward)

The Alerts and custom Health panels are opt-in and profile-driven. A
`/`-prefixed URL in `alertmanager_url` / `health_probes[].url` is fetched via
`kubectl get --raw` through the Kubernetes **API-server proxy**, so it uses your
kubeconfig auth with no localhost port-forward. Health is a self-contained
custom panel plugin (`kutop/plugins/health.py`); the core does not depend on it.

Health probes can be edited live in the app: press `o` (Options), go to the
**Profile** tab, and use the guided health-probe editor to add, remove, and
configure probes — each with a name, URL, and optional label and regex field;
changes apply immediately and persist. Or edit a profile or
`~/.config/kutop/config.yaml` directly:

```yaml
panels:
  keys: true
  health: true
probes:
  health_probes:
    - name: api
      url: /api/v1/namespaces/default/services/api/proxy/health
      fields:
        ready: "ready=(\\w+)"
        latency: "latency_ms=(\\d+)"
    - name: worker
      url: /api/v1/namespaces/default/services/worker/proxy/metrics
      fields:
        lag: "queue_lag=(\\d+)"
```

For a new code-backed custom panel, use the existing plugin seam:

1. Add a module under `kutop/plugins/<name>.py`.
2. Expose a `PLUGIN` object with `panel_id`, `is_enabled(config)`,
   `fetch(fetcher, snapshot)`, `make_panel()`, and `render(panel, snapshot)`.
3. Append the module path to `_BUILTIN_PLUGIN_MODULES` in
   `kutop/plugins/__init__.py`.
4. Keep plugin fetch/render best-effort: a custom panel must never crash the
   main Kubernetes dashboard.

## Troubleshooting

**kutop exits immediately with a message about kubectl not found (exit code 2).**
Cause: `kubectl` is not on PATH. kutop requires a local kubectl for all live
cluster reads and cannot start without one.
Fix: install kubectl (`https://kubernetes.io/docs/tasks/tools/`), ensure it is
on PATH, point it at your cluster (set `KUBECONFIG` or use `~/.kube/config`),
then verify with `kubectl get nodes`. Note: `--self-test`, `--snapshot`, and
`--dump-config` work without kubectl and exit 0 on success.

**The pod table shows rows: `cluster unreachable: … / check: kubectl get nodes / retrying every 5s...`**
Cause: kutop launched but the first cluster snapshot failed — bad kubeconfig,
VPN down, or expired credentials. These rows replace the bare "Loading" row so
you get an actionable hint right away.
Fix: run `kubectl get nodes` from the same shell to diagnose. kutop retries
every 5s and the full dashboard appears automatically once the cluster is
reachable — no restart needed.

**CPU and MEM columns show `-` (or 0).**
Cause: Metrics Server is missing or still warming up.
Fix: run `kubectl top nodes` yourself — if it errors, install Metrics Server
(`kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml`,
or let kutop's startup prompt do it). If it was just installed, wait about a
minute and press `r`.

**`kubectl: command not found` or `current-context is not set`.**
Cause: kubectl is not installed / no kubeconfig context is active.
Fix: install kubectl and verify `kubectl get nodes` works. Point kutop at a
specific cluster with `KUBECONFIG=...` or `kutop --context <name>`.

**An error toast appears and the dashboard stops updating.**
Cause: the cluster became unreachable. kutop intentionally keeps the last
good frame instead of blanking the screen, and shows each distinct error once.
Fix: check connectivity with `kubectl get nodes`; kutop recovers on the next
5s refresh once the cluster is reachable again.

**A `refresh degraded: ...` warning toast.**
Cause: part of the fetch failed — typically one or more namespaces are
Forbidden for your user. Everything that could be fetched is still shown.
The toast now names each failing source (up to 3, e.g. `2 failures: get pods
-n team-a: forbidden; get pvc -n team-b: timeout`), each capped at 60 chars
(`…`), with `+N more` when there are additional failures — so you can fix RBAC
gaps one namespace at a time *(unreleased)*.
Fix: deselect the failing namespace(s), or grant RBAC `get/list` on pods,
nodes, events, and PVCs in each (see
[Requirements & permissions](#requirements--permissions)).

**The PVC panel's USED column shows `-`.**
Cause: PVC usage comes from the kubelet stats summary via the API-server
proxy; a node whose proxy call is denied (or fails) is skipped.
Fix: grant permission for `kubectl get --raw
/api/v1/nodes/<node>/proxy/stats/summary` paths, or ignore — capacity still
renders.

**The Alerts panel says `set probes.alertmanager_url to enable`, or Health
says `no probes configured`.**
Cause: these panels are opt-in and have nothing to read yet.
Fix: configure probes in-app (press `o` → Profile tab → health-probe editor) or
in a profile / `~/.config/kutop/config.yaml` — see
[Alerts & custom panels](#alerts--custom-panels-no-port-forward).

**Pressing `x` says `delete disabled`, or `X` says `restart disabled`.**
Cause: by design. Pod deletion and workload rollout-restart are both gated
behind the sidebar **Allow delete/restart (x/X)** toggle, which always starts off and is
never persisted.
Fix: press `b`, tick **Allow delete/restart (x/X)** (or launch with `--allow-destructive`),
then confirm the popup. The restart confirm (`X`) shows the exact workload
target (`deployment/<name>`, `statefulset/<name>`, etc.) before anything runs.

**A pod delete (`x`) or restart (`X`) shows a failure toast but the message
is cut off.**
Cause: previously, kubectl stderr was truncated at 80 chars, which hid
admission-webhook denials and RBAC messages.
Fix: the toast now shows up to 200 chars of kubectl's stderr (whitespace
collapsed); the full stderr is also written to the Textual devtools log
(`textual console` in a second terminal) *(unreleased)*.

**Summary tiles disappear on a narrow terminal.**
Cause: intentional — whole tiles are dropped to fit rather than wrapping and
corrupting the layout.
Fix: widen the terminal window.

**Start fresh.**
Saved preferences live in `~/.config/kutop/config.yaml`; delete it and kutop
recreates the defaults on the next launch.

## FAQ

**kutop or kubetop — which is it?**
The project, PyPI distribution, and Python package are `kutop`. The `kubetop`
command and `python -m kubetop` ship with kutop purely as compatibility
aliases. The PyPI name `kubetop` belongs to a different, unrelated package —
`pip install kubetop` will not give you this tool. Two related install notes:
`pip install @ken-jo/kutop` is not valid pip syntax (use `pip install kutop`,
or the `kutop @ git+https://...` form for a branch, commit, or tag), and
`pip install "kutop[profiles]"` is accepted for backward compatibility but the
extra is empty — YAML profile support is built in.

**How is kutop different from k9s / ktop / btop?**
kutop is a read-mostly, btop-style *dashboard*: one dense screen of pods,
nodes, CPU/MEM trends, events, PVC usage, and alerts, refreshed on a fixed 5s
cadence through your local `kubectl` with no in-cluster agent. Tools like k9s
are full cluster *management* TUIs with resource navigation and editing. kutop
deliberately keeps mutations to confirm-gated pod delete (`x`) and workload
rollout-restart (`X`) and optimizes the watch-the-cluster experience instead.
btop inspires the look but knows nothing about Kubernetes.

**Is it safe to point at a production cluster?**
kutop is read-mostly. Normal operation only reads (`kubectl get/top`, plus
logs/describe on demand). The two destructive actions — pod delete (`x`) and
workload rollout-restart (`X`) — are double-gated: the sidebar **Allow delete/restart (x/X)**
toggle must be on (it resets to off every launch and is never persisted) and a
confirmation popup must be accepted; the restart confirm spells out the cluster
context, namespace, pod, and the exact workload target before anything runs.
The only other write kutop ever offers is the optional Metrics Server install
at startup, which always asks first and is skipped entirely with
`--no-metrics-bootstrap`.

**Does it work over SSH or in tmux?**
Yes. kutop is a plain terminal app built on Textual; it runs wherever your
terminal and `kubectl` do, including over SSH and inside tmux/screen. On
narrow terminals the summary drops tiles to fit instead of wrapping.

**Why did the "Latest release" badge once show "Unable to select next GitHub
token from pool"?**
That text is a shields.io server-side error, not a kutop problem: badges on
the `img.shields.io/github/*` routes are rendered using shields.io's shared
pool of GitHub API tokens, and when that pool is exhausted shields serves the
error message as the badge image (GitHub's camo proxy can then cache the
broken image for hours). This repo's version badge is now backed by PyPI
(`img.shields.io/pypi/v`), which never touches the GitHub token pool, so it
is immune; the release workflow publishes the same version to PyPI as the
GitHub tag, and the remaining `github/*` badges ask for hourly caching
(`cacheSeconds=3600`) to reduce pool pressure.

## Screenshots

`kutop` can render a headless SVG frame for README images, reviews, and visual
QA. It uses live cluster data when reachable and falls back to a generic
synthetic frame when not. The representative dashboard (every panel enabled:
Summary, Trends, Alerts, custom Health, Pods, Events, PVC) is shown at the top of
this README; the per-tab Options views are below.

Options modal views, one per tab (full width so the controls stay readable):

**View**

![kutop options view tab](docs/kutop-options-view.svg)

**Columns**

![kutop options columns tab](docs/kutop-options-columns.svg)

**Panels**

![kutop options panels tab](docs/kutop-options-panels.svg)

**Thresholds**

![kutop options thresholds tab](docs/kutop-options-thresholds.svg)

**Cluster**

![kutop options cluster tab](docs/kutop-options-cluster.svg)

**Profile**

![kutop options profile tab](docs/kutop-options-profile.svg)

```bash
kutop --snapshot /tmp/kutop.svg
kutop --snapshot /tmp/kutop-wide.svg --detail wide
kutop --snapshot /tmp/kutop-full.svg --detail full
kutop --snapshot /tmp/kutop-full.svg --detail full --size 220x54
kutop --snapshot /tmp/kutop-options.svg --snapshot-view options-panels --size 96x30
```

The detail presets are one-shot column layouts:

| Detail | Default size | Use |
|--------|--------------|-----|
| `normal` | `140x40` | Same visible columns as the interactive default |
| `wide` | `160x44` | Prioritises namespace, readiness, phase, reason, owner, node, and key resources |
| `full` | `220x54` | Enables every table column and the PVC panel; increase `--size` for far-right columns |

`--snapshot-view` accepts `main`, `options-view`, `options-columns`,
`options-panels`, `options-thresholds`, `options-cluster`, and
`options-profile`.

## How it works

* kubectl calls run in a background thread worker; the UI thread never blocks.
  A timer tick is skipped while a fetch is in flight (no thrashing on slow
  clusters), while a namespace/context/profile switch queues an immediate
  refetch and discards any in-flight result from the old scope.
* If the cluster becomes unreachable, the previous frame is kept and the error
  is surfaced as a toast (once per distinct error, not every 5s).
* Node/pod CPU & memory come from `kubectl top` + `kubectl get -o json`.
* PVC usage comes from the kubelet summary API
  (`/api/v1/nodes/<node>/proxy/stats/summary`) because metrics-server does not
  expose it — a node whose summary call fails is skipped, others still report.
* OOMKilled / CrashLoopBackOff / Pending pods are highlighted distinctly; node
  rows lead with the nodegroup (EKS/GKE/AKS label), then the short instance name.

## Contributing & development

```bash
git clone https://github.com/ken-jo/kutop.git
cd kutop
python -m pip install -e ".[test,profiles]"
python -m pytest -q     # full suite, no cluster needed
ruff check              # lint (CI enforces it)
kutop --self-test       # headless smoke test
```

Architecture notes for contributors (and AI assistants) live in
[`llms.txt`](llms.txt) and [`CLAUDE.md`](CLAUDE.md). Release-pipeline setup
(PyPI trusted publishing, the Homebrew tap, apt signing) is maintainer
documentation: [`docs/release.md`](docs/release.md).

## License

MIT. See [LICENSE](LICENSE).
