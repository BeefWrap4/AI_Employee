"""HITL extension tests: supplement, routing, timeout."""
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


def test_supplement_request_marks_task_pending_supplement() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-request",
        json={"question": "请补充传输端口误码计数", "requested_by": "reviewer-alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_supplement"
    assert body["supplement_request"] == "请补充传输端口误码计数"


def test_supplement_answer_returns_to_pending() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-request",
        json={"question": "需要更多信息", "requested_by": "reviewer-alice"},
    )
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-answer",
        json={"answer": "误码为 1e-9", "answered_by": "agent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["supplement_response"] == "误码为 1e-9"


def test_answer_without_supplement_is_conflict() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-answer",
        json={"answer": "premature", "answered_by": "agent"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "not_pending_supplement"


def test_route_assignment_records_new_reviewer() -> None:
    client = _create_rca_run_with_pending_task()
    task_id = _pending_task_id(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/route",
        json={
            "routed_to": "reviewer-bob",
            "routed_by": "reviewer-alice",
            "reason": "primary on leave",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["routed_to"] == "reviewer-bob"


def test_timeout_marks_expired_and_fails_run() -> None:
    client = _create_rca_run_with_pending_task()
    runs = client.get("/api/v1/agent-runs?status=waiting_approval").json()["items"]
    assert runs
    run_id = runs[0]["run_id"]
    task_id = _pending_task_id(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/timeout",
        json={"escalation_reviewer": "reviewer-bob"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"
    assert resp.json()["routed_to"] == "reviewer-bob"
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["approval_status"] == "expired"


def test_supplement_full_loop_back_to_decision() -> None:
    """End-to-end HITL flow: supplement → answer → decide (approve)."""
    client = _create_rca_run_with_pending_task()
    runs = client.get("/api/v1/agent-runs?status=waiting_approval").json()["items"]
    run_id = runs[0]["run_id"]
    task_id = _pending_task_id(client)
    # 1. Reviewer asks for supplement.
    r1 = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-request",
        json={"question": "补全故障时间窗口", "requested_by": "reviewer-alice"},
    )
    assert r1.json()["status"] == "pending_supplement"
    # 2. Agent answers.
    r2 = client.post(
        f"/api/v1/approval-tasks/{task_id}/supplement-answer",
        json={"answer": "窗口为 10:00-10:30", "answered_by": "agent"},
    )
    assert r2.json()["status"] == "pending"
    # 3. Reviewer approves.
    r3 = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "reviewer-alice", "comment": "ok"},
    )
    assert r3.status_code == 200
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"
