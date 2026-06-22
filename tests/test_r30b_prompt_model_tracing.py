"""R30-B: Prompt/Model version cross-service tracing.

Spec §6.4 observability requires every agent run / node trace / audit
event / tool call / ticket write-back to carry the prompt_version and
model_name that produced it, so reviewers can attribute behaviour to a
specific prompt+model pair.

These tests pin the *schema contract* first (item 1 of the round):
each of the five records must accept optional ``prompt_version`` /
``model_name`` fields, defaulting to ``None`` so existing callers keep
working.
"""

from __future__ import annotations

from ai_employee.agent_platform_api.audit import AuditEvent
from ai_employee.agent_platform_api.schemas import (
    AgentRunResponse,
    NodeTrace,
    ToolCallSummary,
)
from ai_employee.rca_agent.ticket_writeback import TicketWritebackRecord


def _baseline_agent_run_response() -> AgentRunResponse:
    return AgentRunResponse(
        run_id="run_1",
        template_id="knowledge_qa",
        agent_name="Knowledge QA Agent",
        status="completed",
        trace_id="trace_1",
        requested_by="alice",
        input={},
        output={},
        node_trace=[],
        tool_calls=[],
        approval_status="not_required",
    )


def test_agent_run_response_accepts_prompt_version_and_model_name() -> None:
    run = _baseline_agent_run_response()
    # Default is None (backward compat).
    assert run.prompt_version is None
    assert run.model_name is None
    run = run.model_copy(update={"prompt_version": "rag-template-v1", "model_name": "qwen-test"})
    assert run.prompt_version == "rag-template-v1"
    assert run.model_name == "qwen-test"


def test_agent_run_response_round_trips_prompt_version_and_model_name() -> None:
    run = _baseline_agent_run_response().model_copy(
        update={"prompt_version": "rag-template-v1", "model_name": "qwen-test"}
    )
    dumped = run.model_dump()
    assert dumped["prompt_version"] == "rag-template-v1"
    assert dumped["model_name"] == "qwen-test"
    # Re-load to confirm field survives serialisation.
    reloaded = AgentRunResponse(**dumped)
    assert reloaded.prompt_version == "rag-template-v1"
    assert reloaded.model_name == "qwen-test"


def test_node_trace_accepts_prompt_version_and_model_name() -> None:
    node = NodeTrace(node_name="RunStarted", status="completed", detail="ok")
    assert node.prompt_version is None
    assert node.model_name is None
    node = NodeTrace(
        node_name="RunStarted",
        status="completed",
        detail="ok",
        prompt_version="rca-template-v1",
        model_name="qwen-test",
    )
    assert node.prompt_version == "rca-template-v1"
    assert node.model_name == "qwen-test"


def test_tool_call_summary_accepts_prompt_version_and_model_name() -> None:
    call = ToolCallSummary(
        tool_name="knowledge-api.chat.query",
        risk_level="read_only",
        status="completed",
    )
    assert call.prompt_version is None
    assert call.model_name is None
    call = ToolCallSummary(
        tool_name="knowledge-api.chat.query",
        risk_level="read_only",
        status="completed",
        prompt_version="rag-template-v1",
        model_name="qwen-test",
    )
    assert call.prompt_version == "rag-template-v1"
    assert call.model_name == "qwen-test"


def test_audit_event_accepts_prompt_version_and_model_name() -> None:
    event = AuditEvent(
        seq=1,
        ts="2026-06-22T00:00:00+00:00",
        actor="alice",
        action="run.created",
        target_type="agent_run",
        target_id="run_1",
    )
    assert event.prompt_version is None
    assert event.model_name is None
    event = AuditEvent(
        seq=1,
        ts="2026-06-22T00:00:00+00:00",
        actor="alice",
        action="run.created",
        target_type="agent_run",
        target_id="run_1",
        prompt_version="rag-template-v1",
        model_name="qwen-test",
    )
    assert event.prompt_version == "rag-template-v1"
    assert event.model_name == "qwen-test"
    # to_dict must surface the new fields.
    assert event.to_dict()["prompt_version"] == "rag-template-v1"
    assert event.to_dict()["model_name"] == "qwen-test"


def test_ticket_writeback_record_accepts_prompt_version_and_model_name() -> None:
    record = TicketWritebackRecord(
        attempt_id="twb_0001",
        ticket_id="T-1001",
        rca_report_id="report_1",
        incident_id="inc_001",
        status="success",
        adapter_name="fixture",
        response={},
        error=None,
        created_at="2026-06-22T00:00:00+00:00",
    )
    assert record.prompt_version is None
    assert record.model_name is None
    record = TicketWritebackRecord(
        attempt_id="twb_0001",
        ticket_id="T-1001",
        rca_report_id="report_1",
        incident_id="inc_001",
        status="success",
        adapter_name="fixture",
        response={},
        error=None,
        created_at="2026-06-22T00:00:00+00:00",
        prompt_version="rca-template-v1",
        model_name="qwen-test",
    )
    assert record.prompt_version == "rca-template-v1"
    assert record.model_name == "qwen-test"
