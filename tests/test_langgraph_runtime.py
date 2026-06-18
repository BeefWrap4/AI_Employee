"""LangGraph v1 runtime tests (spec P3 §4 LangGraph v1).

The :class:`LangGraphRuntime` builds a :class:`StateGraph` that mirrors
the self-built DAG's node-trace semantics:

  TemplateLoaded → RunStarted → ToolPlan → (ApprovalRequired | Completed)

When a template requires approval, the graph pauses at
``ApprovalRequired`` (returns ``waiting_approval``); the existing
approval endpoints then drive the decision.  This keeps the public
HTTP contract identical to the self-built runtime.
"""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.langgraph_runtime import (
    LangGraphRuntime,
    build_langgraph_runtime,
)
from ai_employee.agent_platform_api.schemas import AgentRunCreate


def _payload(template_id: str, *, requires_approval: bool = False) -> AgentRunCreate:
    if template_id == "knowledge_qa":
        return AgentRunCreate(
            template_id="knowledge_qa", requested_by="alice",
            input={"question": "什么是 RRC？"},
        )
    return AgentRunCreate(
        template_id=template_id, requested_by="alice",
        input={"incident_id": "inc_001"},
    )


# --------------------------------------------------------------------------- #
# Graph structure
# --------------------------------------------------------------------------- #


def test_build_runtime_constructs_graph() -> None:
    runtime = build_langgraph_runtime()
    assert runtime is not None
    assert runtime.graph is not None


def test_graph_has_expected_nodes() -> None:
    runtime = LangGraphRuntime()
    # LangGraph compiles nodes into the graph; verify the builder tracked them.
    assert "TemplateLoaded" in runtime.node_names
    assert "RunStarted" in runtime.node_names
    assert "ToolPlan" in runtime.node_names
    assert "ApprovalRequired" in runtime.node_names
    assert "Completed" in runtime.node_names


# --------------------------------------------------------------------------- #
# knowledge_qa — no approval, completes immediately
# --------------------------------------------------------------------------- #


def test_run_no_approval_completes() -> None:
    runtime = LangGraphRuntime()
    result = runtime.run(_payload("knowledge_qa"))
    assert result.status == "completed"
    assert result.approval_status == "not_required"
    node_names = [n.node_name for n in result.node_trace]
    assert "TemplateLoaded" in node_names
    assert "RunStarted" in node_names
    assert "ToolPlan" in node_names
    assert "Completed" in node_names
    # Approval node should NOT appear for no-approval templates.
    assert "ApprovalRequired" not in node_names


def test_run_records_output() -> None:
    runtime = LangGraphRuntime()
    result = runtime.run(_payload("knowledge_qa"))
    assert "summary" in result.output


# --------------------------------------------------------------------------- #
# rca — requires approval, pauses at ApprovalRequired
# --------------------------------------------------------------------------- #


def test_run_with_approval_waits() -> None:
    runtime = LangGraphRuntime()
    result = runtime.run(_payload("rca"))
    assert result.status == "waiting_approval"
    assert result.approval_status == "pending"
    node_names = [n.node_name for n in result.node_trace]
    assert "ApprovalRequired" in node_names
    assert "Completed" not in node_names


def test_run_generates_approval_task() -> None:
    runtime = LangGraphRuntime()
    result = runtime.run(_payload("rca"))
    task = runtime.pending_approval_task(result.run_id)
    assert task is not None
    assert task.status == "pending"
    assert task.run_id == result.run_id


# --------------------------------------------------------------------------- #
# Approval decision advances the graph
# --------------------------------------------------------------------------- #


def test_approve_completes_run() -> None:
    runtime = LangGraphRuntime()
    run = runtime.run(_payload("rca"))
    task = runtime.pending_approval_task(run.run_id)
    assert task is not None
    updated = runtime.decide(run.run_id, decision="approved", decided_by="reviewer", comment="ok")
    assert updated.status == "completed"
    assert updated.approval_status == "approved"
    node_names = [n.node_name for n in updated.node_trace]
    assert "ApprovalApproved" in node_names


def test_reject_fails_run() -> None:
    runtime = LangGraphRuntime()
    run = runtime.run(_payload("rca"))
    updated = runtime.decide(run.run_id, decision="rejected", decided_by="reviewer", comment="no")
    assert updated.status == "failed"
    assert updated.approval_status == "rejected"
    node_names = [n.node_name for n in updated.node_trace]
    assert "ApprovalRejected" in node_names


def test_decide_unknown_run_raises() -> None:
    runtime = LangGraphRuntime()
    with pytest.raises(KeyError):
        runtime.decide("missing", decision="approved", decided_by="x", comment=None)


# --------------------------------------------------------------------------- #
# Trace id + run id shape
# --------------------------------------------------------------------------- #


def test_run_assigns_trace_id() -> None:
    runtime = LangGraphRuntime()
    result = runtime.run(_payload("knowledge_qa"))
    assert result.trace_id.startswith("trace_")


def test_run_id_is_unique() -> None:
    runtime = LangGraphRuntime()
    a = runtime.run(_payload("knowledge_qa"))
    b = runtime.run(_payload("knowledge_qa"))
    assert a.run_id != b.run_id


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def test_build_langgraph_runtime_idempotent() -> None:
    a = build_langgraph_runtime()
    b = build_langgraph_runtime()
    assert a is b
