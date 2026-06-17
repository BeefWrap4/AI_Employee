from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.rca_agent.app import create_app


def _alarm(alarm_id: str, site_id: str, alarm_code: str = "LINK_DEGRADE") -> dict:
    return {
        "alarm_id": alarm_id,
        "alarm_code": alarm_code,
        "alarm_name": alarm_code.replace("_", " ").title(),
        "vendor": "huawei",
        "site_id": site_id,
        "cell_id": f"{site_id}-CELL",
        "ne_id": f"{site_id}-NE",
        "severity": "critical",
        "start_time": "2026-06-17T10:00:00+08:00",
        "raw_payload": {},
    }


def _create_run(client: TestClient, alarm_id: str, site_id: str) -> dict:
    response = client.post(
        "/api/v1/rca/runs",
        json={"alarms": [_alarm(alarm_id, site_id)], "require_human_review": True},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_list_rca_runs_filters_by_status_and_paginates() -> None:
    client = TestClient(create_app())
    accepted = _create_run(client, "a_001", "SITE-001")
    need_more = _create_run(client, "a_002", "SITE-002")
    _create_run(client, "a_003", "SITE-003")

    client.post(
        f"/api/v1/rca/reports/{accepted['report_id']}/review",
        json={"decision": "accepted", "final_root_cause": "confirmed"},
    )
    client.post(
        f"/api/v1/rca/reports/{need_more['report_id']}/review",
        json={"decision": "need_more_evidence", "comment": "collect extra logs"},
    )

    accepted_list = client.get("/api/v1/rca/runs?status=accepted")
    assert accepted_list.status_code == 200
    assert accepted_list.json()["total"] == 1
    assert accepted_list.json()["items"][0]["run_id"] == accepted["run_id"]
    assert accepted_list.json()["items"][0]["status"] == "accepted"

    page = client.get("/api/v1/rca/runs?page=1&page_size=2")
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2
    assert page.json()["page"] == 1
    assert page.json()["page_size"] == 2


def test_list_rca_reports_filters_by_review_status() -> None:
    client = TestClient(create_app())
    rejected = _create_run(client, "a_101", "SITE-101")
    pending = _create_run(client, "a_102", "SITE-102")

    client.post(
        f"/api/v1/rca/reports/{rejected['report_id']}/review",
        json={"decision": "rejected", "comment": "insufficient evidence"},
    )

    rejected_list = client.get("/api/v1/rca/reports?review_status=rejected")
    assert rejected_list.status_code == 200
    assert rejected_list.json()["total"] == 1
    assert rejected_list.json()["items"][0]["report_id"] == rejected["report_id"]
    assert rejected_list.json()["items"][0]["review_status"] == "rejected"

    pending_list = client.get("/api/v1/rca/reports?review_status=pending")
    assert pending_list.status_code == 200
    assert pending_list.json()["total"] == 1
    assert pending_list.json()["items"][0]["report_id"] == pending["report_id"]
