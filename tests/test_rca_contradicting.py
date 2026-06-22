"""RCA contradicting_evidence_ids population tests (spec §6.5)."""

from __future__ import annotations

from ai_employee.rca_agent.app import create_app
from fastapi.testclient import TestClient


def _alarms(code: str = "LINK_DEGRADE"):
    return [
        {
            "alarm_id": "a_001",
            "alarm_code": code,
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


def test_hypotheses_carry_contradicting_evidence() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": True,
            "alarms": _alarms(),
        },
    )
    assert resp.status_code == 201, resp.text
    hypotheses = resp.json()["hypotheses"]
    # Each hypothesis carries a non-empty contradicting list so the
    # human reviewer can see counter-evidence on both sides.
    for h in hypotheses:
        assert h.get("contradicting_evidence_ids"), (
            f"hypothesis {h['hypothesis_id']} has empty contradicting list"
        )
