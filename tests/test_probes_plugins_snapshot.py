from __future__ import annotations

import json

from kutop.config import HealthProbe
from kutop.model import HealthResult
from kutop.plugins.health import HealthPlugin
from kutop.probes import fetch_alerts, scrape_probe, scrape_probes
from kutop.snapshot import SNAPSHOT_VIEWS, synthetic_snapshot


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
