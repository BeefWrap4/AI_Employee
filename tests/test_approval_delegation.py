"""Approval delegation tests.

Distinguishes *delegation* from *routing* (R6-2):
  - Routing moves ownership to a new reviewer.
  - Delegation adds a co-reviewer; both the original assignee and any
    delegate can decide the task.  The first decision wins.
"""

from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def _create_rca_run_with_pending_task() -> TestClient:
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
    return client


def _pending_task_id(client: TestClient) -> str:
    tasks = client.get("/api/v1/approval-tasks?status=pending").json()["items"]
    assert tasks
    return tasks[0]["task_id"]


def test_delegate_adds_co_reviewer() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={
            "delegate": "reviewer-bob",
            "delegated_by": "reviewer-alice",
            "reason": "out of office",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "reviewer-bob" in body["delegates"]
    # Original assignee is preserved.
    assert body.get("requested_by") == "alice"


def test_multiple_delegates_accumulate() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegate": "reviewer-bob", "delegated_by": "reviewer-alice"},
    )
    client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegate": "reviewer-carol", "delegated_by": "reviewer-alice"},
    )
    task = client.get("/api/v1/approval-tasks?status=pending").json()["items"][0]
    assert set(task["delegates"]) == {"reviewer-bob", "reviewer-carol"}


def test_delegate_can_decide() -> None:
    """A delegate (not the original reviewer) is allowed to decide."""
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegate": "reviewer-bob", "delegated_by": "reviewer-alice"},
    )
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={
            "decision": "approved",
            "decided_by": "reviewer-bob",
            "comment": "ok",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "reviewer-bob"


def test_requested_by_can_still_decide_after_delegation() -> None:
    """Original requester still has decision power when delegated."""
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    # No one is explicitly an "assignee" so the requester (alice) decides
    # via the standard endpoint.  After delegation, decision must still work.
    client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegate": "reviewer-bob", "delegated_by": "reviewer-alice"},
    )
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={
            "decision": "rejected",
            "decided_by": "alice",
            "comment": "no",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_decision_after_delegation_conflict_when_already_decided() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegate": "reviewer-bob", "delegated_by": "reviewer-alice"},
    )
    # First decision wins.
    first = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "reviewer-bob", "comment": "ok"},
    )
    assert first.status_code == 200
    # Second decision is a conflict.
    second = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "rejected", "decided_by": "alice", "comment": "no"},
    )
    assert second.status_code == 409


def test_delegate_missing_field_rejected() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegated_by": "reviewer-alice"},  # missing 'delegate'
    )
    assert resp.status_code == 422


def test_delegate_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/approval-tasks/missing/delegate",
        json={"delegate": "bob", "delegated_by": "alice"},
    )
    assert resp.status_code == 404
