from __future__ import annotations

import json
from pathlib import Path

from kutop.config import HealthProbe
from kutop.fetch import Fetcher
from kutop.model import Event, HealthResult, Node, Pod, PVC
from kutop.plugins.health import HealthPlugin
from kutop.probes import fetch_alerts, scrape_probe, scrape_probes
from kutop.snapshot import SNAPSHOT_VIEWS, render_snapshot, synthetic_snapshot


def test_fetch_alerts_filters_active_alertmanager_payload() -> None:
    payload = [
        {
            "labels": {
                "alertname": "PodRestarting",
                "severity": "warning",
                "pod": "api-0",
            },
            "status": {"state": "active"},
            "startsAt": "2026-05-27T07:10:00Z",
        },
        {
            "labels": {"alertname": "Inhibited", "severity": "info"},
            "status": {"state": "suppressed"},
        },
        {"labels": {"severity": "critical"}, "status": {"state": "active"}},
    ]

    alerts = fetch_alerts(
        "http://alertmanager.example/api/v2/alerts",
        getter=lambda _url, _timeout: json.dumps(payload),
    )

    assert len(alerts) == 1
    assert alerts[0].name == "PodRestarting"
    assert alerts[0].severity == "warning"
    assert alerts[0].resource == "api-0"


def test_scrape_probe_extracts_fields_and_handles_failures() -> None:
    ok = scrape_probe(
        "api",
        "/api",
        {"ready": r"ready=(\w+)", "latency": r"latency_ms=(\d+)"},
        getter=lambda _url, _timeout: "ready=true latency_ms=42",
    )
    assert ok.ok is True
    assert ok.fields == {"ready": "true", "latency": "42"}

    missing = scrape_probe("worker", "/worker", {}, getter=lambda *_: None)
    assert missing.ok is False
    assert missing.error == "unreachable"

    probes = [HealthProbe(name="api", url="/api", fields={"ready": r"ready=(\w+)"})]
    results = scrape_probes(probes, getter=lambda *_: "ready=true")
    assert results == [HealthResult(name="api", ok=True, fields={"ready": "true"})]


def test_health_plugin_render_seam_updates_custom_panel() -> None:
    class DummyPanel:
        rows = None

        def update_health(self, rows):
            self.rows = rows

    snapshot = type(
        "SnapshotLike",
        (),
        {"health": [HealthResult(name="api", ok=True, fields={"ready": "true"})]},
    )()
    panel = DummyPanel()

    HealthPlugin().render(panel, snapshot)

    assert panel.rows == [HealthResult(name="api", ok=True, fields={"ready": "true"})]


def test_health_plugin_panel_keeps_common_chrome_class() -> None:
    panel = HealthPlugin().make_panel()

    assert panel.has_class("kpanel")
    assert panel.has_class("-hidden")

    panel.remove_class("-hidden")

    assert panel.has_class("kpanel")
    assert not panel.has_class("-hidden")


def test_pod_resources_sum_all_containers() -> None:
    class FakeFetcher(Fetcher):
        def _run_safe(self, *args: str) -> str:
            cmd = " ".join(args)
            if cmd == "top pods -n default --no-headers --containers":
                return "\n".join(
                    [
                        "indexer-indexer-fe app 100m 128Mi",
                        "indexer-indexer-fe worker 250m 256Mi",
                        "indexer-indexer-fe sidecar 50m 64Mi",
                    ]
                )
            if cmd == "get pods -n default -o json":
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "indexer-indexer-fe",
                                    "creationTimestamp": "2026-06-02T00:00:00Z",
                                },
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "app",
                                            "resources": {
                                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                                "limits": {"cpu": "500m", "memory": "512Mi"},
                                            },
                                        },
                                        {
                                            "name": "worker",
                                            "resources": {
                                                "requests": {"cpu": "200m", "memory": "256Mi"},
                                                "limits": {"cpu": "1", "memory": "1Gi"},
                                            },
                                        },
                                        {
                                            "name": "sidecar",
                                            "resources": {
                                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                                "limits": {"cpu": "100m", "memory": "128Mi"},
                                            },
                                        },
                                    ]
                                },
                                "status": {
                                    "phase": "Running",
                                    "containerStatuses": [
                                        {"name": "app", "ready": True, "restartCount": 1},
                                        {"name": "worker", "ready": True, "restartCount": 2},
                                        {"name": "sidecar", "ready": False, "restartCount": 3},
                                    ],
                                },
                            }
                        ]
                    }
                )
            return ""

    pod = FakeFetcher(["default"])._fetch_pods()[0]

    assert pod.cpu_mcpu == 400
    assert pod.mem_mi == 448
    assert pod.cpu_req_mcpu == 350
    assert pod.cpu_cap_mcpu == 1600
    assert pod.mem_req_mi == 448
    assert pod.mem_cap_mi == 1664
    assert pod.ready == "2/3"
    assert pod.restarts == 6


def test_fetch_core_returns_first_paint_before_auxiliary_panels() -> None:
    class FakeFetcher(Fetcher):
        def _fetch_nodes(self) -> dict[str, Node]:
            return {
                "node-a": Node(
                    name="node-a",
                    ready=True,
                    cpu_mcpu=100,
                    cpu_cap_mcpu=1000,
                    mem_mi=200,
                    mem_cap_mi=1000,
                )
            }

        def _fetch_pods(self) -> list[Pod]:
            return [
                Pod(
                    name="pod-a",
                    namespace="default",
                    node="node-a",
                    phase="Running",
                    cpu_mcpu=10,
                    cpu_cap_mcpu=100,
                    mem_mi=20,
                    mem_cap_mi=100,
                )
            ]

        def _fetch_events(self) -> list[Event]:
            return [
                Event(
                    ts_utc="2026-06-02T00:00:00Z",
                    name="pod-a",
                    reason="Started",
                    message="started",
                    count=1,
                    type="Warning",
                )
            ]

        def _fetch_pvcs(self) -> list[PVC]:
            return [PVC(name="data-pod-a", namespace="default", capacity_mi=100)]

        def _node_summaries(self, node_names: list[str]) -> dict[str, dict]:
            return {}

    fetcher = FakeFetcher(["default"])

    core = fetcher.fetch_core()

    assert core.nodes
    assert core.pods
    assert core.nodes[0].pod_count == 1
    assert core.events == []
    assert core.pvcs == []
    assert core.summary.nodes_total == 1
    assert core.summary.warn_events == 0

    full = fetcher.enrich_snapshot(core)

    assert full.events
    assert full.pvcs
    assert full.summary.warn_events == 1


def test_synthetic_snapshot_covers_all_documented_panels() -> None:
    snap = synthetic_snapshot()

    assert snap.alerts
    assert snap.health
    assert snap.events
    assert snap.pvcs
    assert snap.summary.alerts_firing == len(snap.alerts)
    assert "main" in SNAPSHOT_VIEWS
    assert "options-panels" in SNAPSHOT_VIEWS
    assert "options-profile" in SNAPSHOT_VIEWS


def test_main_snapshot_keeps_sidebar_sections_and_keys_visible(tmp_path: Path) -> None:
    out = tmp_path / "kutop.svg"

    assert render_snapshot(str(out), size=(120, 40), namespaces=["default"]) == 0
    svg = out.read_text(encoding="utf-8")
    assert "SIDEBAR" in svg
    assert "refresh" in svg

    # Keys is intentionally fixed at the bottom; the controls above it are the
    # scrollable region. The leading section headers should remain visible in the
    # default viewport, while lower toggles (SORT checkboxes, PANELS, ACTIONS)
    # remain reachable by scrolling instead of being forced to fit at once. The
    # PROFILE selector leads the controls as the top-level "which workload" pick,
    # and the KEYS panel stays docked at the bottom regardless of scroll.
    for label in (
        "PROFILE",
        "NAMESPACES",
        "SORT",
        "KEYS",
    ):
        assert label in svg
