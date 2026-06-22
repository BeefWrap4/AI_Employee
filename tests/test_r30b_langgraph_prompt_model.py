"""R30-B item 2: LangGraph runtime writes model_name + prompt_version.

Spec §6.4 requires every agent run / node trace / tool call to carry
the prompt_version and model_name that produced it.  The
:class:`LangGraphRuntime` RunStarted node already calls
``LlmClient.chat`` (R29-B); R30-B extends it to capture
``ChatResponse.model`` (the model_name) and the template's declared
prompt_version, then propagate both onto:

* ``AgentRunResponse.model_name`` / ``prompt_version``
* the ``RunStarted`` :class:`NodeTrace` (model_name + prompt_version)
* every ``ToolCallSummary`` produced by the ToolPlan node
* the ``tool_call_log`` row written by ``PlatformToolCallLogStore``

These tests pin the propagation end-to-end using fakes (no network).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import LangGraphRuntime
from ai_employee.agent_platform_api.schemas import AgentRunCreate
from ai_employee.llm_gateway.client import ChatResponse


class FakeLlmClient:
    """Drop-in LlmClient for the runtime's ``chat`` surface."""

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


def _payload(template_id: str) -> AgentRunCreate:
    return AgentRunCreate(
        template_id=template_id,
        requested_by="alice",
        input={"question": "什么是 RRC？"}
        if template_id == "knowledge_qa"
        else {"incident_id": "inc_001"},
    )


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


def test_run_response_carries_model_name_and_prompt_version(
    _isolated_tool_log: Path,
) -> None:
    """The RunStarted node must propagate ``ChatResponse.model`` and the
    template's prompt_version onto the AgentRunResponse."""
    llm = FakeLlmClient(content="answer", model="qwen-plus")
    runtime = LangGraphRuntime(llm_client=llm, mcp_client=FakeMcpGatewayClient())
    result = runtime.run(_payload("knowledge_qa"))
    assert result.model_name == "qwen-plus"
    # knowledge_qa maps to the rag-template-v1 prompt (mirrors knowledge-api).
    assert result.prompt_version == "rag-template-v1"


def test_run_started_node_trace_carries_model_and_prompt_version(
    _isolated_tool_log: Path,
) -> None:
    """The RunStarted NodeTrace must carry model_name + prompt_version so
    reviewers can attribute the node's LLM call to a prompt+model pair."""
    llm = FakeLlmClient(content="answer", model="qwen-plus")
    runtime = LangGraphRuntime(llm_client=llm, mcp_client=FakeMcpGatewayClient())
    result = runtime.run(_payload("knowledge_qa"))
    started = next(n for n in result.node_trace if n.node_name == "RunStarted")
    assert started.model_name == "qwen-plus"
    assert started.prompt_version == "rag-template-v1"


def test_tool_call_summary_carries_model_and_prompt_version(
    _isolated_tool_log: Path,
) -> None:
    """Each ToolCallSummary produced by the ToolPlan node must carry the
    run's model_name + prompt_version (spec §6.4 — tool_call_log
    attribution)."""
    mcp = FakeMcpGatewayClient(results={"knowledge-api.chat.query": {"answer": "ok"}})
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(model="qwen-plus"), mcp_client=mcp)
    result = runtime.run(_payload("knowledge_qa"))
    assert result.tool_calls
    for call in result.tool_calls:
        assert call.model_name == "qwen-plus"
        assert call.prompt_version == "rag-template-v1"


def test_tool_call_log_row_carries_model_and_prompt_version(
    _isolated_tool_log: Path,
) -> None:
    """The persisted tool_call_log row must carry model_name +
    prompt_version so the downstream observability join (spec §6.4) can
    attribute tool latency / success to a prompt+model pair."""
    from ai_employee.agent_platform_api.tool_call_log import (
        PlatformToolCallLogStore,
    )

    mcp = FakeMcpGatewayClient(results={"knowledge-api.chat.query": {"answer": "ok"}})
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(model="qwen-plus"), mcp_client=mcp)
    result = runtime.run(_payload("knowledge_qa"))
    store = PlatformToolCallLogStore()
    rows = store.list_for_run(result.run_id)
    assert rows, "expected at least one tool_call_log row"
    assert rows[0]["model_name"] == "qwen-plus"
    assert rows[0]["prompt_version"] == "rag-template-v1"


def test_each_template_has_distinct_prompt_version(
    _isolated_tool_log: Path,
) -> None:
    """Every one of the 5 templates must resolve to a non-None
    prompt_version so no run is left unattributed."""
    from ai_employee.agent_platform_api.runtime import TEMPLATES

    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(model="qwen-test"),
        mcp_client=FakeMcpGatewayClient(),
    )
    seen: dict[str, str] = {}
    for template_id in TEMPLATES:
        payload = AgentRunCreate(
            template_id=template_id,
            requested_by="alice",
            input={"question": "q"} if template_id == "knowledge_qa" else {"incident_id": "inc_1"},
        )
        result = runtime.run(payload)
        assert result.prompt_version is not None, template_id
        seen[template_id] = result.prompt_version
    # knowledge_qa keeps the knowledge-api convention.
    assert seen["knowledge_qa"] == "rag-template-v1"
    # Each template resolves to a distinct prompt_version label.
    assert len(set(seen.values())) == len(seen)
