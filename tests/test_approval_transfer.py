"""R20-2 approval transfer (reassign) governance tests.

Spec §5.4 HITL transfer (R20 governance flavour):
  POST /api/v1/approvals/{task_id}/transfer  {new_approver, reason, transferred_by, is_admin?}
  State: pending -> transferred (-> pending after new approver picks up)
  Permission: only the current approver / requested_by or an admin may transfer.
  History: every transfer appends to ``transfers`` (chronological).
"""
from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def _client_with_pending_task() -> tuple[TestClient, str]:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "alice",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert resp.status_code == 201
    task = client.get("/api/v1/approval-tasks?status=pending").json()["items"][0]
    return client, task["task_id"]


def test_transfer_records_new_approver_and_history() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={
            "new_approver": "reviewer-bob",
            "reason": "primary on leave",
            "transferred_by": "alice",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "transferred"
    assert body["current_approver"] == "reviewer-bob"
    assert body["routed_to"] == "reviewer-bob"
    assert len(body["transfers"]) == 1
    entry = body["transfers"][0]
    assert entry["from"] == "alice"
    assert entry["to"] == "reviewer-bob"
    assert entry["reason"] == "primary on leave"
    assert entry["transferred_by"] == "alice"
    assert entry["is_admin"] is False


def test_transfer_accumulates_history_across_multiple_reassignments() -> None:
    client, task_id = _client_with_pending_task()
    # alice -> bob
    client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={"new_approver": "reviewer-bob", "reason": "r1", "transferred_by": "alice"},
    )
    # bob -> carol (current approver is now bob)
    r2 = client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={"new_approver": "reviewer-carol", "reason": "r2", "transferred_by": "reviewer-bob"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["current_approver"] == "reviewer-carol"
    assert len(body["transfers"]) == 2
    assert body["transfers"][1]["from"] == "reviewer-bob"
    assert body["transfers"][1]["to"] == "reviewer-carol"


def test_transfer_forbidden_for_unauthorised_actor() -> None:
    """An actor who is neither the current approver nor an admin is rejected."""
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={
            "new_approver": "reviewer-bob",
            "reason": "hijack attempt",
            "transferred_by": "mallory",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "approval_transfer_forbidden"
    # Task is unchanged.
    task = client.get("/api/v1/approval-tasks?status=pending").json()["items"][0]
    assert task["status"] == "pending"
    assert task["current_approver"] == "alice"


def test_admin_can_transfer_without_being_current_approver() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={
            "new_approver": "reviewer-bob",
            "reason": "admin override",
            "transferred_by": "admin-root",
            "is_admin": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_approver"] == "reviewer-bob"
    assert body["transfers"][0]["is_admin"] is True


def test_transfer_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/approvals/missing/transfer",
        json={"new_approver": "bob", "reason": "x", "transferred_by": "alice"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "approval_task_not_found"


def test_transfer_missing_reason_rejected() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={"new_approver": "reviewer-bob", "transferred_by": "alice"},  # missing reason
    )
    assert resp.status_code == 422


def test_transfer_on_already_decided_task_returns_409() -> None:
    client, task_id = _client_with_pending_task()
    client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    resp = client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={"new_approver": "reviewer-bob", "reason": "late", "transferred_by": "alice"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "approval_task_not_transferable"


def test_transferred_task_can_be_decided_by_new_approver() -> None:
    """After transfer the new approver may decide; the run completes."""
    client, task_id = _client_with_pending_task()
    run_id = client.get(
        "/api/v1/agent-runs?status=waiting_approval",
    ).json()["items"][0]["run_id"]
    client.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={"new_approver": "reviewer-bob", "reason": "r", "transferred_by": "alice"},
    )
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "reviewer-bob", "comment": "ok"},
    )
    assert resp.status_code == 200
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"
