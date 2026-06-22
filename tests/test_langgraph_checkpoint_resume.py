"""R31-B: LangGraph checkpointer resume (spec P3 §3 / §4).

R29-B connected the real node bodies (LLM + MCP) and the conditional
edge from ToolPlan, but the "resumable" half of the spec — a
LangGraph :class:`MemorySaver` checkpointer that pauses the run at the
HITL gate and resumes it after the approval decision — was still
missing.  The pre-R31 :meth:`LangGraphRuntime.decide` bypassed the
graph engine entirely, stitching the decision onto the response via
``model_copy`` (R24 audit G6).

This module pins the new checkpointer + resume contract:

  * :meth:`LangGraphRuntime.run` compiles the graph with
    ``checkpointer=MemorySaver(), interrupt_before=["ApprovalRequired"]``.
  * An approval-required run pauses *before* the ApprovalRequired node;
    the response surfaces ``waiting_approval`` and the thread state is
    persisted under ``thread_id = run_id``.
  * :meth:`LangGraphRuntime.resume` injects the decision via
    ``graph.update_state`` and then calls ``graph.invoke(None, config)``
    so the graph engine itself drives the run to completion — the
    ApprovalRequired node runs, the conditional edge routes to
    ApprovalApproved / ApprovalRejected, and the run finalises.
  * Read-only templates never hit the interrupt and complete in one
    ``invoke``.

The legacy :meth:`decide` is kept as a fallback for the
``RUNTIME_BACKEND=langgraph`` + checkpointer-failure path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import (
    LangGraphRuntime,
)
from ai_employee.agent_platform_api.schemas import AgentRunCreate
from ai_employee.llm_gateway.client import ChatResponse

# --------------------------------------------------------------------------- #
# Fakes (mirror test_langgraph_runtime_node_execution.py so the real node
# bodies execute — the checkpointer must persist the same state shape the
# R29-B node bodies produce, not a stub).
# --------------------------------------------------------------------------- #


class FakeLlmClient:
    """Records every chat invocation; returns a configured response."""

    def __init__(
        self,
        *,
        content: str = "LLM drafted an answer.",
        model: str = "fake-model",
    ) -> None:
        self.content = content
        self.model = model
        self.calls: list[list[dict[str, str]]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        *,
        parent_trace_id: str | None = None,
    ) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(
            content=self.content,
            model=self.model,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class FakeMcpGatewayClient:
    """Records every ``invoke_tool`` call; returns configured results."""

    def __init__(
        self,
        *,
        results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        return self._results.get(tool_name, {"ok": True, "tool": tool_name})


def _payload(template_id: str) -> AgentRunCreate:
    if template_id == "knowledge_qa":
        return AgentRunCreate(
            template_id="knowledge_qa",
            requested_by="alice",
            input={"question": "什么是 RRC？"},
        )
    return AgentRunCreate(
        template_id=template_id,
        requested_by="alice",
        input={"incident_id": "inc_001"},
    )


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point PlatformToolCallLogStore at a tmp SQLite file."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


def _runtime() -> LangGraphRuntime:
    """A runtime wired with real fakes so node bodies actually execute."""
    return LangGraphRuntime(
        llm_client=FakeLlmClient(content="RCA draft summary."),
        mcp_client=FakeMcpGatewayClient(),
    )


# --------------------------------------------------------------------------- #
# 1. Approval-required run pauses at the interrupt
# --------------------------------------------------------------------------- #


def test_approval_required_run_pauses_at_interrupt(
    _isolated_tool_log: Path,
) -> None:
    """An approval-required run must pause *before* the ApprovalRequired
    node, surface ``waiting_approval``, and persist the thread state so
    a subsequent resume can pick it up."""
    runtime = _runtime()
    result = runtime.run(_payload("rca"))
    # The run pauses rather than completing.
    assert result.status == "waiting_approval"
    assert result.approval_status == "pending"
    node_names = [n.node_name for n in result.node_trace]
    # The HITL gate is visible in the trace; the Completed / decision
    # nodes are NOT (the run has not resumed yet).
    assert "ApprovalRequired" in node_names
    assert "Completed" not in node_names
    assert "ApprovalApproved" not in node_names
    assert "ApprovalRejected" not in node_names
    # The checkpointer has a persisted thread for this run.
    assert runtime.has_checkpoint(result.run_id)
    # The persisted thread is still parked at the interrupt (next node
    # is ApprovalRequired) — resume is required to drive it forward.
    assert runtime.next_node(result.run_id) == "ApprovalRequired"


# --------------------------------------------------------------------------- #
# 2. Resume after approval completes the run
# --------------------------------------------------------------------------- #


def test_resume_after_approval_completes_run(
    _isolated_tool_log: Path,
) -> None:
    """Resuming an approved run drives the graph to completion: the
    ApprovalRequired node runs, the conditional edge routes to
    ApprovalApproved, and the run finalises as ``completed``."""
    runtime = _runtime()
    paused = runtime.run(_payload("rca"))
    assert paused.status == "waiting_approval"

    resumed = runtime.resume(
        paused.run_id,
        decision="approved",
        decided_by="reviewer",
        comment="LGTM",
    )
    assert resumed.status == "completed"
    assert resumed.approval_status == "approved"
    node_names = [n.node_name for n in resumed.node_trace]
    assert "ApprovalApproved" in node_names
    # The ApprovalRequired node actually executed during resume (it is
    # not a model_copy stitch — the graph engine ran it).
    assert "ApprovalRequired" in node_names
    # Thread is no longer parked at the interrupt.
    assert runtime.next_node(paused.run_id) in (
        None,
        "",
    )


# --------------------------------------------------------------------------- #
# 3. Resume with reject terminates the run as failed
# --------------------------------------------------------------------------- #


def test_resume_reject_terminates_run(_isolated_tool_log: Path) -> None:
    """A rejected resume routes to ApprovalRejected and the run ends as
    ``failed`` with ``approval_status == "rejected"``."""
    runtime = _runtime()
    paused = runtime.run(_payload("rca"))
    resumed = runtime.resume(
        paused.run_id,
        decision="rejected",
        decided_by="reviewer",
        comment="Nope",
    )
    assert resumed.status == "failed"
    assert resumed.approval_status == "rejected"
    node_names = [n.node_name for n in resumed.node_trace]
    assert "ApprovalRejected" in node_names
    assert "ApprovalApproved" not in node_names


# --------------------------------------------------------------------------- #
# 4. Checkpointer persists state across resume (process-level durability)
# --------------------------------------------------------------------------- #


def test_checkpointer_persists_state_across_resume(
    _isolated_tool_log: Path,
) -> None:
    """The MemorySaver checkpointer must persist the paused thread so a
    fresh runtime built on the *same* checkpointer can resume the run.

    This pins the mechanism (state lives on the checkpointer, not on a
    transient runtime attribute) that production RedisSaver /
    PostgresSaver backends will rely on for cross-replica resume.
    """
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    runtime_a = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=FakeMcpGatewayClient(),
        checkpointer=saver,
    )
    paused = runtime_a.run(_payload("rca"))
    assert paused.status == "waiting_approval"

    # A brand-new runtime sharing the same checkpointer can resume the
    # paused run — the thread state was persisted, not held in the
    # original runtime's process-local dict.
    runtime_b = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=FakeMcpGatewayClient(),
        checkpointer=saver,
    )
    resumed = runtime_b.resume(
        paused.run_id,
        decision="approved",
        decided_by="reviewer",
        comment=None,
    )
    assert resumed.status == "completed"
    assert resumed.approval_status == "approved"
    node_names = [n.node_name for n in resumed.node_trace]
    assert "ApprovalApproved" in node_names


# --------------------------------------------------------------------------- #
# 5. Read-only run completes without hitting the interrupt
# --------------------------------------------------------------------------- #


def test_readonly_run_completes_without_interrupt(
    _isolated_tool_log: Path,
) -> None:
    """A read-only template (knowledge_qa) must complete in a single
    ``invoke`` and never park at the ApprovalRequired interrupt."""
    runtime = _runtime()
    result = runtime.run(_payload("knowledge_qa"))
    assert result.status == "completed"
    assert result.approval_status == "not_required"
    node_names = [n.node_name for n in result.node_trace]
    assert "Completed" in node_names
    assert "ApprovalRequired" not in node_names
    # No interrupt parked for read-only runs.
    assert runtime.next_node(result.run_id) in (
        None,
        "",
    )


# --------------------------------------------------------------------------- #
# 6. resume() on an unknown / non-paused run raises
# --------------------------------------------------------------------------- #


def test_resume_unknown_run_raises(_isolated_tool_log: Path) -> None:
    """Resuming a run id with no persisted checkpoint raises KeyError."""
    runtime = _runtime()
    with pytest.raises(KeyError):
        runtime.resume(
            "lg_run_missing",
            decision="approved",
            decided_by="x",
            comment=None,
        )
