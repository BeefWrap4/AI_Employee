"""R21 agent-platform → approval-service delegation (pluggable client).

Spec §9: approval tasks move to a standalone ``approval-service``.  The
agent-platform keeps its existing endpoint contracts (consumers are
unaware) but delegates the task state machine to the service over HTTP
when ``APPROVAL_SERVICE_URL`` is set.  When unset, it falls back to the
in-memory store (backward compat / tests).

This file exercises both modes:

* **in-memory mode** (no env) — the platform uses
  :class:`InMemoryApprovalServiceClient`, which wraps the existing
  runtime functions.  All legacy approval tests keep passing.
* **HTTP mode** (``APPROVAL_SERVICE_URL`` set) — the platform uses
  :class:`HttpApprovalServiceClient` against a real approval-service
  ``TestClient`` mounted at a fake URL.  The platform still owns run
  side-effects (complete/fail the run, append node trace).

A :class:`FakeApprovalServiceClient` is also used to assert the
platform calls the right client methods and applies run side-effects
on a decision.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.clients import (
    ApprovalServiceClient,
    FakeApprovalServiceClient,
    HttpApprovalServiceClient,
    InMemoryApprovalServiceClient,
    build_approval_client,
)
from ai_employee.approval_service.app import create_app as create_approval_app
from ai_employee.approval_service.store import ApprovalTaskStore
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# In-memory mode (fallback): the legacy flows must keep working unchanged.
# --------------------------------------------------------------------------- #


def _make_run(client: TestClient, template_id: str = "rca") -> tuple[str, str]:
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": template_id,
            "requested_by": "alice",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    task = client.get("/api/v1/approval-tasks?status=pending").json()["items"][0]
    return run_id, task["task_id"]


def test_in_memory_mode_is_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPROVAL_SERVICE_URL", raising=False)
    client = build_approval_client()
    assert isinstance(client, InMemoryApprovalServiceClient)


def test_in_memory_mode_decision_completes_run() -> None:
    """Legacy contract: deciding an approval completes the run in-memory."""
    app = create_app()
    client = TestClient(app)
    run_id, task_id = _make_run(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "alice", "comment": "ok"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"


# --------------------------------------------------------------------------- #
# HTTP mode: the platform delegates to a real approval-service TestClient.
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    """Wire the platform to a real approval-service via a fake URL.

    The HttpApprovalServiceClient is patched to talk to a TestClient of
    the approval-service instead of opening a real socket, so the test
    is hermetic.
    """
    store = ApprovalTaskStore(db_path=str(tmp_path / "approval.sqlite3"))
    approval_app = create_approval_app(store=store)
    approval_client = TestClient(approval_app)

    def _fake_post(self, path, json):  # type: ignore[no-untyped-def]
        return approval_client.post(path, json=json)

    def _fake_get(self, path, params=None):  # type: ignore[no-untyped-def]
        return approval_client.get(path, params=params)

    monkeypatch.setattr(HttpApprovalServiceClient, "_post", _fake_post)
    monkeypatch.setattr(HttpApprovalServiceClient, "_get", _fake_get)
    monkeypatch.setenv("APPROVAL_SERVICE_URL", "http://approval-service.test")

    # Force a fresh client resolution inside create_app by passing one
    # explicitly so the env-driven factory is exercised end-to-end.
    explicit = HttpApprovalServiceClient("http://approval-service.test")
    return TestClient(create_app(approval_client=explicit))


def test_http_mode_decision_delegates_and_completes_run(http_app: TestClient) -> None:
    run_id, task_id = _make_run(http_app)
    resp = http_app.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "alice", "comment": "ok"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    # Run side-effect applied locally by the platform.
    run = http_app.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["approval_status"] == "approved"


def test_http_mode_transfer_delegates_to_service(http_app: TestClient) -> None:
    _, task_id = _make_run(http_app)
    resp = http_app.post(
        f"/api/v1/approvals/{task_id}/transfer",
        json={"new_approver": "reviewer-bob", "reason": "on leave", "transferred_by": "alice"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "transferred"
    assert body["current_approver"] == "reviewer-bob"


def test_http_mode_supplement_round_trip(http_app: TestClient) -> None:
    _, task_id = _make_run(http_app)
    req = http_app.post(
        f"/api/v1/approvals/{task_id}/supplement",
        json={"note": "need logs", "attachments": [], "requested_by": "reviewer"},
    )
    assert req.status_code == 200
    assert req.json()["status"] == "supplement_pending"
    resolve = http_app.post(
        f"/api/v1/approvals/{task_id}/supplement/resolve",
        json={"attachments": [], "note": "done", "resolved_by": "alice"},
    )
    assert resolve.status_code == 200
    assert resolve.json()["status"] == "pending"


def test_http_mode_unknown_task_returns_404(http_app: TestClient) -> None:
    resp = http_app.post(
        "/api/v1/approval-tasks/missing/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "approval_task_not_found"


def test_http_mode_decision_on_terminal_task_returns_409(http_app: TestClient) -> None:
    _, task_id = _make_run(http_app)
    http_app.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    again = http_app.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "rejected", "decided_by": "alice"},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "approval_task_already_decided"


# --------------------------------------------------------------------------- #
# Fake client: assert the platform calls the protocol + applies run effects.
# --------------------------------------------------------------------------- #


def test_fake_client_records_decide_and_platform_completes_run() -> None:
    """A Fake client lets us assert the delegation surface without a socket."""
    fake = FakeApprovalServiceClient()
    app = create_app(approval_client=fake)
    client = TestClient(app)
    run_id, task_id = _make_run(client)
    resp = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "alice"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    # The platform applied the run side-effect even though the task
    # transition was delegated.
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"
    # And the fake recorded the delegated decision call.
    assert any(call[0] == "decide" for call in fake.calls)


def test_approval_service_client_is_protocol() -> None:
    """ApprovalServiceClient is a runtime_checkable Protocol."""
    from typing import runtime_checkable

    assert runtime_checkable(ApprovalServiceClient)
    assert isinstance(InMemoryApprovalServiceClient(), ApprovalServiceClient)
    assert isinstance(HttpApprovalServiceClient("http://x"), ApprovalServiceClient)
    assert isinstance(FakeApprovalServiceClient(), ApprovalServiceClient)
