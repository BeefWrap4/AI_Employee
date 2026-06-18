from __future__ import annotations

from ai_employee.rca_agent.app import create_app
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(create_app())


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
            "raw_payload": {"port": "eth0/1", "ber": "high"},
        },
        {
            "alarm_id": "a_002",
            "alarm_code": "RRC_SETUP_FAIL_HIGH",
            "alarm_name": "RRC setup failure rate high",
            "vendor": "huawei",
            "site_id": "SITE-001",
            "cell_id": "CELL-001",
            "ne_id": "NE-001",
            "severity": "major",
            "start_time": "2026-06-17T10:02:00+08:00",
            "raw_payload": {"rrc_fail_rate": 0.23},
        },
    ]


def test_create_rca_run_from_alarm_replay_generates_evidence_and_report() -> None:
    client = _client()

    created = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 10,
            "require_human_review": True,
            "alarms": _sample_alarms(),
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["run_id"].startswith("rca_run_")
    assert body["incident_id"].startswith("inc_")
    assert body["report_id"].startswith("rca_report_")
    assert body["status"] == "waiting_review"
    assert body["current_node"] == "HumanReview"
    assert body["evidence_count"] >= 5
    assert body["hypotheses"][0]["root_cause_type"] == "transmission_link_degradation"
    assert body["hypotheses"][0]["supporting_evidence_ids"]

    fetched = client.get(f"/api/v1/rca/runs/{body['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["trace_id"] == body["trace_id"]
    assert fetched.json()["state_history"][-1] == "HumanReview"

    report = client.get(f"/api/v1/rca/reports/{body['report_id']}")
    assert report.status_code == 200
    report_body = report.json()
    assert "## Top-N 根因候选" in report_body["report_markdown"]
    assert body["hypotheses"][0]["supporting_evidence_ids"][0] in report_body["report_markdown"]


def test_alarm_event_and_incident_build_endpoints_normalize_and_group_alarms() -> None:
    client = _client()

    alarm_resp = client.post("/api/v1/alarms/events", json=_sample_alarms()[0])
    assert alarm_resp.status_code == 201, alarm_resp.text
    alarm = alarm_resp.json()
    assert alarm["alarm_event_id"].startswith("alarm_evt_")
    assert alarm["severity"] == "critical"
    assert alarm["fingerprint"] == "huawei:SITE-001:NE-001:LINK_DEGRADE"

    incident_resp = client.post(
        "/api/v1/incidents/build",
        json={"alarms": _sample_alarms(), "time_window_minutes": 30},
    )
    assert incident_resp.status_code == 201, incident_resp.text
    incident = incident_resp.json()
    assert incident["incident_id"].startswith("inc_")
    assert incident["primary_alarm"]["alarm_code"] == "LINK_DEGRADE"
    assert incident["related_alarm_count"] == 1
    assert incident["site_id"] == "SITE-001"


def test_report_review_records_human_decision() -> None:
    client = _client()
    created = client.post(
        "/api/v1/rca/runs",
        json={"alarms": _sample_alarms(), "require_human_review": True},
    ).json()

    reviewed = client.post(
        f"/api/v1/rca/reports/{created['report_id']}/review",
        json={
            "decision": "accepted",
            "final_root_cause": "Transmission link errors caused access failures.",
            "reviewer": "ops_expert",
            "comment": "Confirmed by transport team.",
        },
    )

    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["review_status"] == "accepted"
    assert body["final_root_cause"] == "Transmission link errors caused access failures."

    report = client.get(f"/api/v1/rca/reports/{created['report_id']}").json()
    assert report["review_status"] == "accepted"
    assert report["final_root_cause"] == "Transmission link errors caused access failures."
