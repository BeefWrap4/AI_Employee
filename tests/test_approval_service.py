"""R21 approval-service: independent approval task persistence + state machine.

Spec §9 lists ``approval-service`` as a standalone deployable unit that
owns approval task persistence, the state machine, and governance
(supplement / transfer / escalation).  These tests exercise the service
directly over HTTP (via ``TestClient``) and confirm:

* tasks persist to SQLite across restarts (``ApprovalTaskStore``)
* the full governance lifecycle mirrors the R20 contracts:
  - ``POST /api/v1/approval-tasks`` — create a task
  - ``GET /api/v1/approval-tasks`` — list (with status filter + paging)
  - ``POST /api/v1/approval-tasks/{id}/decision``
  - ``POST /api/v1/approvals/{id}/supplement`` + ``.../supplement/resolve``
  - ``POST /api/v1/approvals/{id}/transfer``
  - ``POST /api/v1/approvals/{id}/escalate``
* 404 / 409 / 403 state guards match the agent-platform contracts so
  consumers can switch to the service with no contract drift.

The service is exercised in isolation — it does NOT touch agent runs
(run side-effects remain the platform's responsibility).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ai_employee.approval_service.app import create_app
from ai_employee.approval_service.store import ApprovalTaskStore
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path: Path) -> ApprovalTaskStore:
    """A fresh on-disk store per test so tests never share task state."""
    return ApprovalTaskStore(db_path=str(tmp_path / "approval.sqlite3"))


@pytest.fixture
def client(store: ApprovalTaskStore) -> TestClient:
    return TestClient(create_app(store=store))


def _create_task(
    client: TestClient,
    *,
    task_id: str = "approval_task_001",
    run_id: str = "agent_run_001",
    template_id: str = "rca",
    requested_by: str = "alice",
) -> dict:
    resp = client.post(
        "/api/v1/approval-tasks",
        json={
            "task_id": task_id,
            "run_id": run_id,
            "template_id": template_id,
            "requested_by": requested_by,
            "risk_level": "approval_required",
            "reason": "Human approval required before final write-back.",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_list_approval_tasks(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1")
    _create_task(client, task_id="t2", run_id="r2", requested_by="bob")

    listed = client.get("/api/v1/approval-tasks")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 2
    ids = {item["task_id"] for item in body["items"]}
    assert ids == {"t1", "t2"}

    pending = client.get("/api/v1/approval-tasks?status=pending")
    assert pending.json()["total"] == 2


def test_decision_transitions_task_to_approved(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1")
    resp = client.post(
        "/api/v1/approval-tasks/t1/decision",
        json={"decision": "approved", "decided_by": "alice", "comment": "ok"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "alice"
    assert body["comment"] == "ok"


def test_decision_on_terminal_task_returns_409(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1")
    client.post(
        "/api/v1/approval-tasks/t1/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    again = client.post(
        "/api/v1/approval-tasks/t1/decision",
        json={"decision": "rejected", "decided_by": "alice"},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "approval_task_already_decided"


def test_decision_unknown_task_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/approval-tasks/missing/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "approval_task_not_found"


def test_supplement_governance_round_trip(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1")
    req = client.post(
        "/api/v1/approvals/t1/supplement",
        json={
            "note": "need logs",
            "attachments": [{"name": "syslog", "uri": "file:///logs/sys"}],
            "requested_by": "reviewer",
        },
    )
    assert req.status_code == 200, req.text
    assert req.json()["status"] == "supplement_pending"

    resolve = client.post(
        "/api/v1/approvals/t1/supplement/resolve",
        json={
            "attachments": [{"name": "extra", "uri": "file:///logs/extra"}],
            "note": "attached",
            "resolved_by": "alice",
        },
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "pending"
    assert len(body["supplement_attachments"]) == 2


def test_transfer_records_history_and_new_approver(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1", requested_by="alice")
    resp = client.post(
        "/api/v1/approvals/t1/transfer",
        json={"new_approver": "reviewer-bob", "reason": "on leave", "transferred_by": "alice"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "transferred"
    assert body["current_approver"] == "reviewer-bob"
    assert len(body["transfers"]) == 1
    assert body["transfers"][0]["to"] == "reviewer-bob"


def test_transfer_forbidden_for_unauthorised_actor(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1", requested_by="alice")
    resp = client.post(
        "/api/v1/approvals/t1/transfer",
        json={"new_approver": "bob", "reason": "x", "transferred_by": "mallory"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "approval_transfer_forbidden"


def test_escalate_marks_task_escalated(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1", requested_by="alice")
    resp = client.post(
        "/api/v1/approvals/t1/escalate",
        json={"escalated_to": "lead", "reason": "overdue", "escalated_by": "alice"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "escalated"
    assert body["escalated_to"] == "lead"
    assert body["escalated_at"] is not None


def test_escalate_on_terminal_task_returns_409(client: TestClient) -> None:
    _create_task(client, task_id="t1", run_id="r1")
    client.post(
        "/api/v1/approval-tasks/t1/decision",
        json={"decision": "rejected", "decided_by": "alice"},
    )
    resp = client.post(
        "/api/v1/approvals/t1/escalate",
        json={"escalated_to": "lead", "reason": "late", "escalated_by": "alice"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "approval_task_not_escalatable"


def test_tasks_persist_across_app_restart(tmp_path: Path) -> None:
    """A task written by one app instance must survive a fresh instance
    backed by the same SQLite file (proves the persistence boundary)."""
    db_path = tmp_path / "approval.sqlite3"
    store = ApprovalTaskStore(db_path=str(db_path))
    app1 = create_app(store=store)
    _create_task(TestClient(app1), task_id="t1", run_id="r1")

    # New store + app pointing at the same DB file.
    store2 = ApprovalTaskStore(db_path=str(db_path))
    app2 = create_app(store=store2)
    listed = TestClient(app2).get("/api/v1/approval-tasks")
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["task_id"] == "t1"


def test_store_uses_existing_connection_when_provided(tmp_path: Path) -> None:
    """The store accepts an external sqlite3 connection (test isolation)."""
    db_path = tmp_path / "approval.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    store = ApprovalTaskStore(connection=conn)
    store.init_schema()
    store.upsert(
        {
            "task_id": "t1",
            "run_id": "r1",
            "template_id": "rca",
            "requested_by": "alice",
            "status": "pending",
            "risk_level": "approval_required",
            "reason": "x",
            "created_at": "2026-06-17T00:00:00Z",
            "updated_at": "2026-06-17T00:00:00Z",
        }
    )
    rows, total = store.list()
    assert total == 1
    assert rows[0]["task_id"] == "t1"
    conn.close()
