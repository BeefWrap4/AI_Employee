"""R20-3 approval escalation governance tests.

Spec §5.4 HITL timeout escalation (R20 governance flavour):
  - Background scheduler scans pending tasks older than
    APPROVAL_TIMEOUT_SECONDS (default 3600) and escalates them.
  - Escalation action: notify the escalation reviewer + mark status
    ``escalated``.
  - Manual trigger: POST /api/v1/approvals/{task_id}/escalate
    {escalated_to?, reason?, escalated_by?}
"""
from __future__ import annotations

import time

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.escalation import (
    escalate_overdue_approvals,
    notify_escalation_reviewer,
)
from ai_employee.agent_platform_api.runtime import AgentPlatformStore, create_run
from ai_employee.agent_platform_api.schemas import AgentRunCreate
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


# --------------------------------------------------------------------------- #
# Manual escalation endpoint
# --------------------------------------------------------------------------- #


def test_manual_escalate_marks_task_escalated() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/escalate",
        json={
            "escalated_to": "escalation-lead",
            "reason": "reviewer unresponsive",
            "escalated_by": "shift_lead",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "escalated"
    assert body["escalated_to"] == "escalation-lead"
    assert body["escalation_reason"] == "reviewer unresponsive"
    assert body["escalated_at"] is not None
    assert body["current_approver"] == "escalation-lead"


def test_manual_escalate_defaults_to_current_approver_when_target_missing() -> None:
    client, task_id = _client_with_pending_task()
    resp = client.post(
        f"/api/v1/approvals/{task_id}/escalate",
        json={"reason": "sla breach", "escalated_by": "shift_lead"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "escalated"
    # No explicit target -> falls back to current approver (alice).
    assert body["escalated_to"] == "alice"


def test_manual_escalate_unknown_task_returns_404() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/approvals/missing/escalate",
        json={"reason": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "approval_task_not_found"


def test_manual_escalate_on_already_decided_returns_409() -> None:
    client, task_id = _client_with_pending_task()
    client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    resp = client.post(
        f"/api/v1/approvals/{task_id}/escalate",
        json={"reason": "late"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "approval_task_not_escalatable"


# --------------------------------------------------------------------------- #
# Background scheduler
# --------------------------------------------------------------------------- #


def _store_with_pending_task(created_at_iso: str | None = None) -> tuple[AgentPlatformStore, str]:
    store = AgentPlatformStore()
    run = create_run(
        store,
        AgentRunCreate(template_id="rca", requested_by="alice", input={"incident_id": "inc"}),
    )
    task_id = next(
        tid for tid, t in store.approval_tasks.items() if t.run_id == run.run_id
    )
    if created_at_iso is not None:
        task = store.approval_tasks[task_id]
        store.approval_tasks[task_id] = task.model_copy(update={"created_at": created_at_iso})
    return store, task_id


def test_scheduler_escalates_overdue_pending_task() -> None:
    """A pending task older than the SLA threshold is escalated."""
    # created_at 2 hours ago -> beyond the 3600s default threshold.
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store, task_id = _store_with_pending_task(old)
    escalated = escalate_overdue_approvals(
        store, timeout_seconds=3600, escalate_to="escalation-lead",
    )
    assert task_id in escalated
    task = store.approval_tasks[task_id]
    assert task.status == "escalated"
    assert task.escalated_to == "escalation-lead"


def test_scheduler_skips_fresh_pending_task() -> None:
    """A just-created task is within the SLA window and not escalated."""
    store, task_id = _store_with_pending_task()
    escalated = escalate_overdue_approvals(
        store, timeout_seconds=3600, escalate_to="escalation-lead",
    )
    assert escalated == []
    assert store.approval_tasks[task_id].status == "pending"


def test_scheduler_skips_already_decided_task_even_if_old() -> None:
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store, task_id = _store_with_pending_task(old)
    # Approve first.
    from ai_employee.agent_platform_api.runtime import decide_approval_task

    decide_approval_task(
        store, task_id=task_id, decision="approved",
        decided_by="alice", comment="ok",
    )
    escalated = escalate_overdue_approvals(
        store, timeout_seconds=3600, escalate_to="escalation-lead",
    )
    assert escalated == []
    assert store.approval_tasks[task_id].status == "approved"


def test_scheduler_respects_custom_timeout_threshold() -> None:
    """A 1-second threshold escalates a task created a moment ago."""
    store, task_id = _store_with_pending_task()
    # Wait just over a second so the task is older than 1s.
    time.sleep(1.05)
    escalated = escalate_overdue_approvals(
        store, timeout_seconds=1, escalate_to="escalation-lead",
    )
    assert task_id in escalated
    assert store.approval_tasks[task_id].status == "escalated"


def test_scheduler_notifies_escalation_reviewer() -> None:
    """The escalation action must notify the escalation reviewer."""
    notifications: list[dict] = []
    notify_escalation_reviewer(
        task_id="approval_task_001",
        escalated_to="escalation-lead",
        reason="sla breach",
        notifier=notifications.append,
    )
    assert len(notifications) == 1
    notice = notifications[0]
    assert notice["task_id"] == "approval_task_001"
    assert notice["escalated_to"] == "escalation-lead"
    assert notice["reason"] == "sla breach"
    assert "ts" in notice


# --------------------------------------------------------------------------- #
# APPROVAL_TIMEOUT_SECONDS env default
# --------------------------------------------------------------------------- #


def test_default_timeout_is_3600_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_employee.agent_platform_api import escalation

    monkeypatch.delenv("APPROVAL_TIMEOUT_SECONDS", raising=False)
    assert escalation.default_timeout_seconds() == 3600


def test_timeout_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_employee.agent_platform_api import escalation

    monkeypatch.setenv("APPROVAL_TIMEOUT_SECONDS", "120")
    assert escalation.default_timeout_seconds() == 120
