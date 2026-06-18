from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def _create_rca_run(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "ops_expert",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_rca_run_creates_pending_approval_task_and_can_be_approved() -> None:
    client = TestClient(create_app())
    run = _create_rca_run(client)

    tasks = client.get("/api/v1/approval-tasks")
    assert tasks.status_code == 200
    body = tasks.json()
    assert body["total"] == 1
    task = body["items"][0]
    assert task["task_id"].startswith("approval_task_")
    assert task["run_id"] == run["run_id"]
    assert task["status"] == "pending"
    assert task["risk_level"] == "approval_required"

    decided = client.post(
        f"/api/v1/approval-tasks/{task['task_id']}/decision",
        json={
            "decision": "approved",
            "decided_by": "shift_lead",
            "comment": "RCA report can be finalized.",
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    fetched_run = client.get(f"/api/v1/agent-runs/{run['run_id']}")
    assert fetched_run.json()["status"] == "completed"
    assert fetched_run.json()["approval_status"] == "approved"
    assert fetched_run.json()["node_trace"][-1]["node_name"] == "ApprovalApproved"


def test_approval_rejection_fails_waiting_run() -> None:
    client = TestClient(create_app())
    run = _create_rca_run(client)
    task = client.get("/api/v1/approval-tasks").json()["items"][0]

    decided = client.post(
        f"/api/v1/approval-tasks/{task['task_id']}/decision",
        json={"decision": "rejected", "decided_by": "shift_lead"},
    )

    assert decided.status_code == 200
    assert decided.json()["status"] == "rejected"
    fetched_run = client.get(f"/api/v1/agent-runs/{run['run_id']}").json()
    assert fetched_run["status"] == "failed"
    assert fetched_run["approval_status"] == "rejected"
    assert fetched_run["node_trace"][-1]["node_name"] == "ApprovalRejected"


def test_approval_task_filters_and_rejects_duplicate_decision() -> None:
    client = TestClient(create_app())
    _create_rca_run(client)
    _create_rca_run(client)

    pending = client.get("/api/v1/approval-tasks?status=pending&page=1&page_size=1")
    assert pending.status_code == 200
    assert pending.json()["total"] == 2
    assert len(pending.json()["items"]) == 1
    task_id = pending.json()["items"][0]["task_id"]

    first = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "shift_lead"},
    )
    assert first.status_code == 200

    duplicate = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "rejected", "decided_by": "shift_lead"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["error_code"] == "approval_task_already_decided"
