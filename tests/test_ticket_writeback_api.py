"""End-to-end API tests for the RCA ticket write-back endpoint."""

from __future__ import annotations

from ai_employee.rca_agent.app import create_app
from ai_employee.rca_agent.ticket_writeback import FixtureTicketWritebackAdapter
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
            "raw_payload": {},
        }
    ]


def _accepted_report_id(client: TestClient) -> str:
    created = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 10,
            "require_human_review": False,
            "alarms": _sample_alarms(),
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["report_id"]


def test_writeback_endpoint_creates_attempt_record() -> None:
    client = _client()
    report_id = _accepted_report_id(client)
    resp = client.post(
        "/api/v1/tickets/T-001/rca-summary",
        json={"rca_report_id": report_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == "T-001"
    assert body["rca_report_id"] == report_id
    assert body["status"] == "success"
    assert body["adapter_name"] == FixtureTicketWritebackAdapter().name
    assert body["attempt_id"].startswith("twb_")

    listing = client.get("/api/v1/tickets/T-001/rca-summary/attempts")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "success"


def test_writeback_returns_404_for_unknown_report() -> None:
    client = _client()
    resp = client.post(
        "/api/v1/tickets/T-002/rca-summary",
        json={"rca_report_id": "rca_report_does_not_exist"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "rca_report_not_found"
