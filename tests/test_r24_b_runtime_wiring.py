"""LangGraph runtime wiring tests (R24-B.5).

The agent platform's ``create_run`` was a dead switch — even when
``RUNTIME_BACKEND=langgraph`` was set, the platform kept using the
self-built DAG.  After R24-B.5:

* ``RUNTIME_BACKEND=dag`` (default) → self-built DAG, runs return
  ``agent_run_NNN`` ids and an in-store approval task for
  approval-required templates.
* ``RUNTIME_BACKEND=langgraph`` → LangGraphRuntime drives the run and
  the result is mirrored back into the platform store under the same
  ``agent_run_NNN`` id scheme so trace / list / resume endpoints keep
  working.

These tests verify both code paths at the runtime level and through
the HTTP layer.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import LangGraphRuntime
from ai_employee.agent_platform_api.runtime import (
    AgentPlatformStore,
    create_run,
    select_runtime,
)
from ai_employee.agent_platform_api.schemas import AgentRunCreate


def _payload(template_id: str = "knowledge_qa") -> AgentRunCreate:
    return AgentRunCreate(
        template_id=template_id,
        requested_by="alice",
        input={"question": "hi"},
    )


def test_select_runtime_returns_none_for_default_dag_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default backend is ``dag`` → ``select_runtime()`` returns None."""
    monkeypatch.delenv("RUNTIME_BACKEND", raising=False)
    assert select_runtime() is None


def test_select_runtime_returns_langgraph_for_langgraph_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env=langgraph → ``select_runtime()`` returns a LangGraphRuntime."""
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    runtime = select_runtime()
    assert isinstance(runtime, LangGraphRuntime)


def test_create_run_dag_path_unchanged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default backend keeps the existing DAG behaviour (id prefix, approval task)."""
    monkeypatch.delenv("RUNTIME_BACKEND", raising=False)
    store = AgentPlatformStore()
    run = create_run(store, _payload("rca"))  # approval-required template
    assert run.run_id.startswith("agent_run_")
    assert run.status == "waiting_approval"
    assert run.approval_status == "pending"
    assert run.run_id in store.runs
    # Approval task was created in the platform store.
    assert any(
        t.run_id == run.run_id and t.status == "pending" for t in store.approval_tasks.values()
    )


def test_create_run_langgraph_path_mirrors_into_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """env=langgraph → run goes through LangGraphRuntime but uses agent_run_NNN."""
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    runtime = select_runtime()
    assert runtime is not None
    store = AgentPlatformStore()
    run = create_run(store, _payload("knowledge_qa"), runtime=runtime)
    # Public id follows the platform scheme even though the runtime
    # internally minted ``lg_run_NNN``.
    assert run.run_id.startswith("agent_run_")
    assert run.run_id in store.runs
    # LangGraph node names must surface in the response.
    node_names = [n.node_name for n in run.node_trace]
    assert "TemplateLoaded" in node_names
    assert "RunStarted" in node_names
    assert "ToolPlan" in node_names
    assert "Completed" in node_names


def test_create_run_langgraph_approval_required_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """env=langgraph + rca template → ApprovalRequired node, mirrored task."""
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    runtime = select_runtime()
    store = AgentPlatformStore()
    run = create_run(store, _payload("rca"), runtime=runtime)
    assert run.run_id.startswith("agent_run_")
    assert run.status == "waiting_approval"
    assert run.approval_status == "pending"
    node_names = [n.node_name for n in run.node_trace]
    assert "ApprovalRequired" in node_names
    # Approval task was mirrored into the platform store.
    assert any(
        t.run_id == run.run_id and t.status == "pending" for t in store.approval_tasks.values()
    )


def test_create_run_langgraph_id_counter_advances_with_platform_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple LangGraph runs share the platform's monotonic counter."""
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    runtime = select_runtime()
    store = AgentPlatformStore()
    a = create_run(store, _payload("knowledge_qa"), runtime=runtime)
    b = create_run(store, _payload("knowledge_qa"), runtime=runtime)
    assert a.run_id != b.run_id
    assert a.run_id.startswith("agent_run_")
    assert b.run_id.startswith("agent_run_")
    # The LangGraph runtime internally uses its own counter starting at
    # 001; the platform ids here come from the AgentPlatformStore and
    # are independent.  Just assert the platform's two are distinct.
    assert store.run_count == 2


# --------------------------------------------------------------------------- #
# HTTP-layer integration
# --------------------------------------------------------------------------- #


def test_http_create_run_uses_langgraph_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """env=langgraph + POST /api/v1/agent-runs → run driven via LangGraph."""
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    from ai_employee.agent_platform_api.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "hi"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Same public id scheme as the DAG path.
    assert body["run_id"].startswith("agent_run_")
    assert body["trace_id"].startswith("trace_")
    # LangGraph node sequence must be reflected in the trace.
    node_names = [n["node_name"] for n in body["node_trace"]]
    assert "TemplateLoaded" in node_names
    assert "RunStarted" in node_names
    assert "ToolPlan" in node_names
    assert "Completed" in node_names
    # GET /trace endpoint stays consistent (legacy contract preserved).
    trace = client.get(f"/api/v1/agent-runs/{body['run_id']}/trace")
    assert trace.status_code == 200
    assert trace.json()["run"]["run_id"] == body["run_id"]


def test_http_create_run_keeps_dag_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default backend keeps the DAG-driven behaviour (no ApprovalRequired node for read-only)."""
    monkeypatch.delenv("RUNTIME_BACKEND", raising=False)
    from ai_employee.agent_platform_api.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "hi"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["run_id"].startswith("agent_run_")
    assert body["status"] == "completed"
