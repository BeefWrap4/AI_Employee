"""RCA operational metrics tests."""
from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.rca_agent.app import create_app
from ai_employee.rca_agent.metrics import compute_metrics
from ai_employee.rca_agent.runtime import RcaStore


def _sample_alarms() -> list[dict]:
    return [
        {
            "alarm_id": "a_001",
            "alarm_code": "LINK_DEGRADE",
            "alarm_name": "Transmission link degradation",
            "vendor": "huawei",
            "site_id": "SITE-001",
            "cell_id": "CELL-001",
            "ne_id": "NE-001",
            "severity": "critical",
            "start_time": "2026-06-17T10:00:00+08:00",
            "raw_payload": {},
        }
    ]


def test_compute_metrics_with_empty_store() -> None:
    store = RcaStore()
    metrics = compute_metrics(store)
    # No failures recorded → tool success rate is 100%.
    assert metrics.tool_call_success_rate == 1.0
    # No reviews yet → 0.0 accepted (semantically "no data").
    assert metrics.human_acceptance_rate == 0.0
    # No alarms → compression ratio undefined, defaults to 1.0.
    assert metrics.alert_compression_ratio == 1.0
    assert metrics.report_gen_seconds_avg == 0.0


def test_compute_metrics_with_populated_store() -> None:
    store = RcaStore()
    store.tool_call_attempts = 10
    store.tool_call_failures = 2
    store.reviewed_reports = 5
    store.accepted_reports = 4
    store.alarm_count_total = 12
    store.incident_alarm_total = 3
    store.report_gen_count = 5
    store.report_gen_seconds_total = 7.5

    metrics = compute_metrics(store)
    assert metrics.tool_call_success_rate == 0.8
    assert metrics.human_acceptance_rate == 0.8
    assert metrics.alert_compression_ratio == 0.25
    assert metrics.report_gen_seconds_avg == 1.5


def test_compute_metrics_handles_only_failures() -> None:
    store = RcaStore()
    store.tool_call_attempts = 4
    store.tool_call_failures = 4
    store.reviewed_reports = 3
    store.accepted_reports = 0
    metrics = compute_metrics(store)
    assert metrics.tool_call_success_rate == 0.0
    assert metrics.human_acceptance_rate == 0.0


def test_operational_metrics_endpoint_returns_dict() -> None:
    client = TestClient(create_app())
    # Generate one run so counters are non-default.
    created = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": False,
            "alarms": _sample_alarms(),
        },
    )
    assert created.status_code == 201, created.text
    resp = client.get("/api/v1/metrics/operations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "tool_call_success_rate" in body
    assert "human_acceptance_rate" in body
    assert "alert_compression_ratio" in body
    assert "report_gen_seconds_avg" in body
    assert body["raw"]["report_gen_count"] >= 1


def test_metrics_endpoint_reflects_review_decisions() -> None:
    client = TestClient(create_app())
    created = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": True,
            "alarms": _sample_alarms(),
        },
    )
    run = created.json()
    review = client.post(
        f"/api/v1/rca/reports/{run['report_id']}/review",
        json={"decision": "accepted", "final_root_cause": "transmission_link_degradation"},
    )
    assert review.status_code == 200

    resp = client.get("/api/v1/metrics/operations")
    body = resp.json()
    assert body["raw"]["accepted_reports"] >= 1
    assert body["raw"]["reviewed_reports"] >= 1
