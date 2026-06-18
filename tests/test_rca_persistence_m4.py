from __future__ import annotations

from pathlib import Path

import pytest
from ai_employee.rca_agent.app import create_app
from ai_employee.rca_agent.store import SQLiteRcaStore
from fastapi.testclient import TestClient


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


def _client(db_path: Path) -> TestClient:
    return TestClient(create_app(store=SQLiteRcaStore(str(db_path))))


def test_rca_run_report_and_review_survive_app_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "rca.sqlite3"
    first_client = _client(db_path)

    created = first_client.post(
        "/api/v1/rca/runs",
        json={"alarms": _sample_alarms(), "require_human_review": True},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    report_id = created.json()["report_id"]

    reviewed = first_client.post(
        f"/api/v1/rca/reports/{report_id}/review",
        json={
            "decision": "accepted",
            "final_root_cause": "Transmission link degradation.",
            "reviewer": "ops_expert",
        },
    )
    assert reviewed.status_code == 200, reviewed.text

    second_client = _client(db_path)

    persisted_run = second_client.get(f"/api/v1/rca/runs/{run_id}")
    assert persisted_run.status_code == 200
    assert persisted_run.json()["status"] == "accepted"

    persisted_report = second_client.get(f"/api/v1/rca/reports/{report_id}")
    assert persisted_report.status_code == 200
    assert persisted_report.json()["review_status"] == "accepted"
    assert persisted_report.json()["final_root_cause"] == "Transmission link degradation."


def test_default_app_uses_sqlite_when_env_path_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "rca-env.sqlite3"
    monkeypatch.setenv("RCA_SQLITE_PATH", str(db_path))

    first_client = TestClient(create_app())
    created = first_client.post(
        "/api/v1/rca/runs",
        json={"alarms": _sample_alarms(), "require_human_review": True},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]

    second_client = TestClient(create_app())
    persisted_run = second_client.get(f"/api/v1/rca/runs/{run_id}")
    assert persisted_run.status_code == 200
