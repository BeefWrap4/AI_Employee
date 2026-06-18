"""R20.5 state-machine guard tests for the legacy HITL endpoints.

The R20 governance endpoints (/approvals/{id}/...) already guard terminal
states and return 404 on unknown tasks.  The legacy M5 endpoints
(/approval-tasks/{id}/supplement-request|route|timeout|delegate|decision)
did not — they silently mutated approved/rejected/expired tasks and
raised 500 (KeyError) on unknown task ids.  These tests pin the hardened
behaviour.
"""

from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def _client_with_decided_task(decision: str = "approved") -> tuple[TestClient, str]:
    """Create an RCA run, then drive its approval task to a terminal state."""
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
    task_id = client.get("/api/v1/approval-tasks?status=pending").json()["items"][0]["task_id"]
    r = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": decision, "decided_by": "reviewer-alice", "comment": "ok"},
    )
    assert r.status_code == 200
    return client, task_id


def _client_with_expired_task() -> tuple[TestClient, str]:
    client = TestClient(create_app())
    client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "alice",
            "input": {"incident_id": "inc_001"},
        },
    )
    task_id = client.get("/api/v1/approval-tasks?status=pending").json()["items"][0]["task_id"]
    client.post(
        f"/api/v1/approval-tasks/{task_id}/timeout",
        json={"escalation_reviewer": "reviewer-bob"},
    )
    return client, task_id


# --------------------------------------------------------------------------- #
# Terminal-state guards: approved task must not be revived / mutated
# --------------------------------------------------------------------------- #


def test_supplement_request_on_approved_returns_409() -> None:
    client, task_id = _client_with_decided_task("approved")
    r = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-request",
        json={"question": "more info", "requested_by": "reviewer-alice"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "approval_task_not_supplementable"


def test_route_on_approved_returns_409() -> None:
    client, task_id = _client_with_decided_task("approved")
    r = client.post(
        f"/api/v1/approval-tasks/{task_id}/route",
        json={"routed_to": "reviewer-bob", "routed_by": "alice", "reason": "x"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "approval_task_not_modifiable"


def test_timeout_on_approved_returns_409_and_does_not_fail_run() -> None:
    client, task_id = _client_with_decided_task("approved")
    run_id = client.get("/api/v1/agent-runs?status=completed").json()["items"][0]["run_id"]
    r = client.post(
        f"/api/v1/approval-tasks/{task_id}/timeout",
        json={"escalation_reviewer": "reviewer-bob"},
    )
    assert r.status_code == 409
    # The completed run must NOT be flipped to failed.
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["approval_status"] == "approved"


def test_delegate_on_approved_returns_409() -> None:
    client, task_id = _client_with_decided_task("approved")
    r = client.post(
        f"/api/v1/approval-tasks/{task_id}/delegate",
        json={"delegate": "reviewer-bob", "delegated_by": "alice", "reason": "x"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "approval_task_not_modifiable"


def test_supplement_request_on_expired_returns_409() -> None:
    client, task_id = _client_with_expired_task()
    r = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-request",
        json={"question": "more info", "requested_by": "reviewer-alice"},
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Unknown task id → 404 (not 500)
# --------------------------------------------------------------------------- #


def test_supplement_request_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/approval-tasks/missing/supplement-request",
        json={"question": "x", "requested_by": "r"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "approval_task_not_found"


def test_supplement_answer_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/approval-tasks/missing/supplement-answer",
        json={"answer": "x", "answered_by": "r"},
    )
    assert r.status_code == 404


def test_route_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/approval-tasks/missing/route",
        json={"routed_to": "bob", "routed_by": "alice", "reason": "x"},
    )
    assert r.status_code == 404


def test_timeout_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/approval-tasks/missing/timeout",
        json={"escalation_reviewer": "bob"},
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Runtime-layer guard: decide_approval_task rejects non-decidable directly
# --------------------------------------------------------------------------- #


def test_decide_approval_task_runtime_rejects_terminal() -> None:
    """The runtime function itself (not just the HTTP layer) must guard."""
    import pytest
    from ai_employee.agent_platform_api.runtime import (
        AgentPlatformStore,
        ApprovalTask,
        decide_approval_task,
    )

    store = AgentPlatformStore()
    store.approval_tasks["t1"] = ApprovalTask(
        task_id="t1",
        run_id="r1",
        template_id="rca",
        requested_by="alice",
        status="approved",
        risk_level="approval_required",
        reason="done",
    )
    with pytest.raises(ValueError):
        decide_approval_task(store, task_id="t1", decision="approved", decided_by="x", comment=None)
