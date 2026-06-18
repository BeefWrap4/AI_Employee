"""R20-1 approval supplement governance tests.

Spec §5.4 HITL supplement (R20 governance flavour):
  POST /api/v1/approvals/{task_id}/supplement  {note, attachments}
  State flow: pending -> supplement_pending -> (approver fills) -> pending

This is the governance-track supplement, distinct from the legacy
``/approval-tasks/{id}/supplement-request`` HITL flow (which uses the
``pending_supplement`` status).  R20 standardises the unified status
machine: ``supplement_pending``.
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


def test_supplement_moves_pending_to_supplement_pending() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/supplement",
        json={
            "note": "Please attach the alarm screenshot before 10:30.",
            "attachments": [{"name": "alarms.png", "uri": "file:///tmp/alarms.png"}],
            "requested_by": "reviewer-alice",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "supplement_pending"
    assert body["supplement_note"] == "Please attach the alarm screenshot before 10:30."
    assert len(body["supplement_attachments"]) == 1
    assert body["supplement_attachments"][0]["name"] == "alarms.png"


def test_supplement_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/approvals/missing/supplement",
        json={"note": "x", "requested_by": "alice"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "approval_task_not_found"


def test_supplement_on_already_decided_task_returns_409() -> None:
    client, task_id = _client_with_pending_task()
    # Decide first.
    client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "reviewer-alice"},
    )
    resp = client.post(
        f"/api/v1/approvals/{task_id}/supplement",
        json={"note": "late", "requested_by": "alice"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "approval_task_not_supplementable"


def test_supplement_missing_note_rejected() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/supplement",
        json={"requested_by": "alice"},  # missing 'note'
    )
    assert resp.status_code == 422


def test_supplement_then_resolve_returns_to_pending() -> None:
    """Approver supplies the requested material; task returns to pending."""
    client, task_id = _client_with_pending_task()
    client.post(
        f"/api/v1/approvals/{task_id}/supplement",
        json={
            "note": "Need port error count.",
            "attachments": [],
            "requested_by": "reviewer-alice",
        },
    )
    resp = client.post(
        f"/api/v1/approvals/{task_id}/supplement/resolve",
        json={
            "attachments": [{"name": "err.csv", "uri": "file:///tmp/err.csv"}],
            "resolved_by": "alice",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert len(body["supplement_attachments"]) == 1


def test_supplement_resolve_when_not_supplement_pending_returns_409() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/supplement/resolve",
        json={"resolved_by": "alice"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "not_supplement_pending"


def test_supplement_full_loop_back_to_decision() -> None:
    client, task_id = _client_with_pending_task()
    run_id = client.get(
        "/api/v1/agent-runs?status=waiting_approval",
    ).json()["items"][0]["run_id"]
    # 1. Request supplement.
    r1 = client.post(
        f"/api/v1/approvals/{task_id}/supplement",
        json={"note": "complete the time window", "requested_by": "reviewer-alice"},
    )
    assert r1.json()["status"] == "supplement_pending"
    # 2. Resolve supplement.
    r2 = client.post(
        f"/api/v1/approvals/{task_id}/supplement/resolve",
        json={"resolved_by": "alice"},
    )
    assert r2.json()["status"] == "pending"
    # 3. Approve.
    r3 = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "reviewer-alice", "comment": "ok"},
    )
    assert r3.status_code == 200
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"
