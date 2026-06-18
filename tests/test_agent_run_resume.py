"""Agent Platform run persistence + resume API tests."""

from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.run_store import AgentRunStore
from fastapi.testclient import TestClient


def _client(tmp_path) -> TestClient:
    run_store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    return TestClient(create_app(run_store=run_store))


def test_create_run_persists_to_sqlite(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "What is RRC?"},
        },
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    run_store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    persisted = run_store.get_run(run["run_id"])
    assert persisted is not None
    assert persisted["template_id"] == "knowledge_qa"
    assert persisted["status"] == "completed"


def test_resume_run_requires_approval_template(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "alice",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert created.json()["status"] == "waiting_approval"

    resp = client.post(f"/api/v1/agent-runs/{run_id}/resume")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resumed_from_node"] == "ApprovalRequired"
    assert body["run"]["status"] == "waiting_approval"
    nodes = [n["node_name"] for n in body["run"]["node_trace"]]
    assert "ResumeNode" in nodes


def test_resume_completed_run_returns_409(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {},
        },
    )
    run_id = created.json()["run_id"]
    resp = client.post(f"/api/v1/agent-runs/{run_id}/resume")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "agent_run_already_completed"


def test_resume_unknown_run_returns_404(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post("/api/v1/agent-runs/does_not_exist/resume")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "agent_run_not_found"
