"""R30-B item 3: change_assessment + ticket_summary tool invocation.

Spec §5.5 mandates the 5-template set.  R30-B item 4/5 verify the
LangGraph runtime drives real tool invocation for every template's
declared ``tool_names``.  These two tests pin the contract for the two
templates that were added last (change_assessment + ticket_summary):

* ``change_assessment`` is approval-required, so its tools are
  *planned* (not yet invoked) — the test pins that the plan declares
  ``cmdb.lookup``, ``ticket.history.search`` and
  ``knowledge-api.chat.query`` in order, with status ``planned``.
* ``ticket_summary`` is read-only, so its tools are *invoked* through
  ``mcp_client.invoke_tool`` — the test pins that ``ticket.fetch`` and
  ``knowledge-api.chat.query`` are each called exactly once and the
  resulting ``ToolCallSummary`` rows are ``completed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import LangGraphRuntime
from ai_employee.agent_platform_api.schemas import AgentRunCreate
from ai_employee.llm_gateway.client import ChatResponse


class FakeLlmClient:
    def __init__(self, *, content: str = "draft", model: str = "qwen-test") -> None:
        self.content = content
        self.model = model

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        *,
        parent_trace_id: str | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            content=self.content,
            model=self.model,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class FakeMcpGatewayClient:
    def __init__(self, *, results: dict[str, dict[str, Any]] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        return self._results.get(tool_name, {"ok": True, "tool": tool_name})


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


def test_change_assessment_run_plans_all_three_tools(
    _isolated_tool_log: Path,
) -> None:
    """change_assessment (approval-required) must declare cmdb.lookup,
    ticket.history.search and knowledge-api.chat.query in its tool plan.

    Because the template requires approval, the tools stay ``planned``
    (the HITL gate fires before execution) — but the declared names
    must be present and ordered so the reviewer sees the planned
    evidence-gathering steps.
    """
    mcp = FakeMcpGatewayClient()
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(content="change risk draft", model="qwen-test"),
        mcp_client=mcp,
    )
    result = runtime.run(
        AgentRunCreate(
            template_id="change_assessment",
            requested_by="alice",
            input={
                "change_id": "CR-2026-0618-001",
                "change_type": "parameter",
                "affected_ne_ids": ["NE-001", "NE-002"],
            },
        )
    )
    assert result.status == "waiting_approval"
    planned_names = [t.tool_name for t in result.tool_calls]
    assert planned_names == [
        "cmdb.lookup",
        "ticket.history.search",
        "knowledge-api.chat.query",
    ]
    # Approval-required → tools stay planned until the HITL decision.
    assert all(t.status == "planned" for t in result.tool_calls)
    # The MCP client must NOT have been invoked yet (gate fires first).
    assert mcp.calls == []


def test_ticket_summary_run_invokes_ticket_fetch_and_knowledge_query(
    _isolated_tool_log: Path,
) -> None:
    """ticket_summary (read-only) must invoke ticket.fetch and
    knowledge-api.chat.query through the MCP gateway client.

    R32-B: the ToolPlan step now fans the tools out to a parallel
    subgraph (spec §5.2 "并行子任务"), so the invocation order is no
    longer pinned — the two tools run concurrently.  Each tool is still
    invoked exactly once, and the resulting ``ToolCallSummary`` rows
    move to ``completed``.
    """
    mcp = FakeMcpGatewayClient(
        results={
            "ticket.fetch": {"summary": "ticket T-1001 timeline"},
            "knowledge-api.chat.query": {"answer": "SOP for postmortem."},
        }
    )
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(content="ticket postmortem", model="qwen-test"),
        mcp_client=mcp,
    )
    result = runtime.run(
        AgentRunCreate(
            template_id="ticket_summary",
            requested_by="bob",
            input={"ticket_id": "T-1001"},
        )
    )
    assert result.status == "completed"
    # Each declared tool is invoked exactly once (order is unspecified
    # under parallel subgraph execution).
    invoked_names = [name for name, _ in mcp.calls]
    assert sorted(invoked_names) == ["knowledge-api.chat.query", "ticket.fetch"]
    # Every tool call in the response is completed, in declared order
    # (the ToolAggregate node re-orders by the template's tool_names).
    assert [t.tool_name for t in result.tool_calls] == [
        "ticket.fetch",
        "knowledge-api.chat.query",
    ]
    assert all(t.status == "completed" for t in result.tool_calls)
    # R30-B: each tool summary carries the run's prompt+model pair.
    for call in result.tool_calls:
        assert call.model_name == "qwen-test"
        assert call.prompt_version == "ticket-summary-template-v1"
