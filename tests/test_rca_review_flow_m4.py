from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.rca_agent.app import create_app


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
        }
    ]


def _create_run(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/rca/runs",
        json={"alarms": _sample_alarms(), "require_human_review": True},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_need_more_evidence_review_moves_run_to_need_more_evidence() -> None:
    client = TestClient(create_app())
    created = _create_run(client)

    reviewed = client.post(
        f"/api/v1/rca/reports/{created['report_id']}/review",
        json={
            "decision": "need_more_evidence",
            "comment": "Please collect transmission cutover records.",
        },
    )

    assert reviewed.status_code == 200, reviewed.text
    run = client.get(f"/api/v1/rca/runs/{created['run_id']}").json()
    assert run["status"] == "need_more_evidence"
    assert run["current_node"] == "NeedMoreEvidence"
    assert run["state_history"][-1] == "NeedMoreEvidence"

    report = client.get(f"/api/v1/rca/reports/{created['report_id']}").json()
    assert report["review_status"] == "need_more_evidence"


def test_resume_need_more_evidence_run_appends_evidence_and_returns_to_review() -> None:
    client = TestClient(create_app())
    created = _create_run(client)
    client.post(
        f"/api/v1/rca/reports/{created['report_id']}/review",
        json={"decision": "need_more_evidence", "comment": "Need extra ticket evidence."},
    )

    resumed = client.post(f"/api/v1/rca/runs/{created['run_id']}/resume")

    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["status"] == "waiting_review"
    assert body["current_node"] == "HumanReview"
    assert body["evidence_count"] == created["evidence_count"] + 1
    assert body["state_history"][-3:] == ["CollectEvidence", "GenerateReport", "HumanReview"]

    report = client.get(f"/api/v1/rca/reports/{created['report_id']}").json()
    assert report["review_status"] == "pending"
    assert report["final_root_cause"] is None
    assert any(evidence["evidence_id"] == "e_006" for evidence in report["evidence"])


def test_resume_rejects_runs_that_are_not_waiting_for_more_evidence() -> None:
    client = TestClient(create_app())
    created = _create_run(client)

    resumed = client.post(f"/api/v1/rca/runs/{created['run_id']}/resume")

    assert resumed.status_code == 409
    assert resumed.json()["detail"]["error_code"] == "run_not_waiting_for_more_evidence"
