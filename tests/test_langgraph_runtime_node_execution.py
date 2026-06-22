"""R29-B: Real node execution in the LangGraph v1 runtime.

The :class:`LangGraphRuntime` previously produced fake "plan only"
node-trace semantics — tool calls were never executed, the LLM was
never invoked, and the run output never reflected any model result.
Per spec P3 §3 / §4 the LangGraph nodes must:

  * :func:`_node_run_started` — call ``LlmClient.chat`` with a templated
    prompt, and persist the model content into ``run.output.summary``.
  * :func:`_node_tool_plan` — iterate ``template.tool_names`` and call
    ``mcp_client.invoke_tool(name, args)`` for each.  On success the
    tool call moves to ``status="completed"`` and a row is appended to
    :class:`PlatformToolCallLogStore`; on failure the status becomes
    ``"failed"`` and the row carries an ``error_code``.
  * :func:`_node_approval_required` — still pauses for HITL on
    approval-required templates.
  * :func:`_node_completed` — still marks the run ``completed`` for
    read-only templates.

The dependency-injection surface stays optional so existing callers
(the ``build_langgraph_runtime()`` singleton, the ``RUNTIME_BACKEND``
env switch, and the legacy ``tests/test_langgraph_runtime.py``) keep
working with their fake LLM / no-mcp path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import (
    LangGraphRuntime,
    build_langgraph_runtime,
)
from ai_employee.agent_platform_api.schemas import AgentRunCreate
from ai_employee.llm_gateway.client import ChatResponse, LlmClientError

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeLlmClient:
    """Records every chat invocation; returns a configured response.

    Behaves as a drop-in replacement for :class:`LlmClient` for the
    limited surface (``chat``) the runtime actually uses.  When
    ``raise_error`` is set, ``chat`` raises :class:`LlmClientError`
    instead — used to verify failure handling in the ToolPlan node.
    """

    def __init__(
        self,
        *,
        content: str = "LLM drafted an answer.",
        model: str = "fake-model",
        raise_error: Exception | None = None,
    ) -> None:
        self.content = content
        self.model = model
        self.raise_error = raise_error
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
        if self.raise_error is not None:
            raise self.raise_error
        return ChatResponse(
            content=self.content,
            model=self.model,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class FakeMcpGatewayClient:
    """Records every ``invoke_tool`` call; returns configured results.

    Each ``tool_name`` is mapped to either a successful payload or a
    :class:`RuntimeError` (so we can drive the failure path without
    standing up a real mcp-gateway).
    """

    def __init__(
        self,
        *,
        results: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        self._results = results or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if tool_name in self._errors:
            raise RuntimeError(self._errors[tool_name])
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
    """Point :class:`PlatformToolCallLogStore` at a tmp SQLite file.

    Avoids polluting the developer-machine ``var/data/`` directory
    when the test exercises the real ``record`` write path.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


# --------------------------------------------------------------------------- #
# TemplateLoaded (regression — the new constructor must not break it)
# --------------------------------------------------------------------------- #


def test_template_loaded_node_fills_template_meta() -> None:
    """The TemplateLoaded node still records the template trace detail."""
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=FakeMcpGatewayClient(),
    )
    result = runtime.run(_payload("knowledge_qa"))
    template_loaded = result.node_trace[0]
    assert template_loaded.node_name == "TemplateLoaded"
    assert template_loaded.status == "completed"
    assert "knowledge_qa" in template_loaded.detail


# --------------------------------------------------------------------------- #
# RunStarted — real LLM invocation
# --------------------------------------------------------------------------- #


def test_run_started_node_calls_llm_and_writes_output() -> None:
    """The RunStarted node must call LlmClient.chat and persist the
    model content into ``run.output.summary``."""
    llm = FakeLlmClient(content="这是来自 RAG 的答案。", model="qwen-test")
    runtime = LangGraphRuntime(llm_client=llm, mcp_client=FakeMcpGatewayClient())
    result = runtime.run(_payload("knowledge_qa"))
    assert llm.calls, "LlmClient.chat was never called"
    # Prompt must reference the template input so the LLM has context.
    prompt_blob = " ".join(m.get("content", "") for m in llm.calls[0])
    assert "什么是 RRC" in prompt_blob
    # Run output summary reflects the LLM response verbatim.
    assert result.output["summary"] == "这是来自 RAG 的答案。"
    # RunStarted trace records the LLM interaction.
    started = next(n for n in result.node_trace if n.node_name == "RunStarted")
    assert "qwen-test" in started.detail
    assert started.status == "completed"


def test_run_started_node_handles_llm_failure_gracefully() -> None:
    """When LlmClient raises, the run should still complete (read-only
    templates) but with a degraded summary so observability surfaces
    the failure."""
    llm = FakeLlmClient(
        content="unused",
        raise_error=LlmClientError("upstream 503", status_code=503),
    )
    runtime = LangGraphRuntime(llm_client=llm, mcp_client=FakeMcpGatewayClient())
    result = runtime.run(_payload("knowledge_qa"))
    # Run still completes (read-only template), and the summary flags
    # the LLM failure so callers can surface it.
    assert result.status == "completed"
    assert "LLM error" in result.output["summary"] or "503" in result.output["summary"]


# --------------------------------------------------------------------------- #
# ToolPlan — real MCP tool execution
# --------------------------------------------------------------------------- #


def test_tool_plan_node_invokes_mcp_client_for_each_tool() -> None:
    """Each tool in ``template.tool_names`` must be invoked exactly once."""
    mcp = FakeMcpGatewayClient(
        results={
            "knowledge-api.chat.query": {"answer": "RRC 是无线资源控制协议。"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    runtime.run(_payload("knowledge_qa"))
    assert [name for name, _ in mcp.calls] == ["knowledge-api.chat.query"]


def test_tool_plan_marks_tools_completed_on_success() -> None:
    """Successful tool invocations move ``status`` from ``planned`` to
    ``completed`` in the response."""
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=FakeMcpGatewayClient(
            results={"knowledge-api.chat.query": {"answer": "ok"}},
        ),
    )
    result = runtime.run(_payload("knowledge_qa"))
    assert result.tool_calls
    for t in result.tool_calls:
        assert t.status == "completed"
        assert t.tool_name == "knowledge-api.chat.query"


def test_tool_plan_records_tool_call_log_entries(
    _isolated_tool_log: Path,
) -> None:
    """Every tool execution writes a row to PlatformToolCallLogStore
    so the ``tool_latency_p95`` / ``tool_call_success_rate`` headline
    indicators pick it up."""
    from ai_employee.agent_platform_api.tool_call_log import (
        PlatformToolCallLogStore,
    )

    mcp = FakeMcpGatewayClient(
        results={"knowledge-api.chat.query": {"answer": "x"}},
    )
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=mcp,
    )
    result = runtime.run(_payload("knowledge_qa"))
    store = PlatformToolCallLogStore()
    rows = store.list_for_run(result.run_id)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "knowledge-api.chat.query"
    assert rows[0]["status"] == "success"
    assert rows[0]["latency_ms"] is not None


def test_tool_plan_marks_failed_and_writes_error_code(
    _isolated_tool_log: Path,
) -> None:
    """A tool that raises gets ``status="failed"`` and an
    ``error_code`` in both the response and the tool_call_log row."""
    from ai_employee.agent_platform_api.tool_call_log import (
        PlatformToolCallLogStore,
    )

    mcp = FakeMcpGatewayClient(
        errors={"knowledge-api.chat.query": "upstream timeout"},
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_payload("knowledge_qa"))
    assert result.tool_calls[0].status == "failed"
    store = PlatformToolCallLogStore()
    rows = store.list_for_run(result.run_id)
    assert rows[0]["status"] == "failure"
    assert rows[0]["error_code"] == "tool_invocation_error"


# --------------------------------------------------------------------------- #
# ApprovalRequired — still pauses for HITL
# --------------------------------------------------------------------------- #


def test_approval_required_node_still_pauses() -> None:
    """Approval-required templates still pause at the ApprovalRequired
    node; the new LLM/MCP wiring must not bypass the HITL gate."""
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(content="RCA draft."),
        mcp_client=FakeMcpGatewayClient(),
    )
    result = runtime.run(_payload("rca"))
    assert result.status == "waiting_approval"
    assert result.approval_status == "pending"
    node_names = [n.node_name for n in result.node_trace]
    assert "ApprovalRequired" in node_names
    assert "Completed" not in node_names
    # No tool was invoked yet (HITL gate fires before tool execution).
    # ``rca`` has two tools in its template; neither should be in
    # the response's tool_calls list, or they should remain planned.
    statuses = {t.status for t in result.tool_calls}
    assert statuses.issubset({"planned"})


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #


def test_end_to_end_langgraph_run_produces_real_llm_output(
    _isolated_tool_log: Path,
) -> None:
    """A read-only run driven through the langgraph runtime should
    produce an output that contains real LLM content and a tool_call_log
    entry for the executed tool."""
    llm = FakeLlmClient(content="Answer: RRC 是无线资源控制协议。")
    mcp = FakeMcpGatewayClient(
        results={"knowledge-api.chat.query": {"answer": "ok"}},
    )
    runtime = LangGraphRuntime(llm_client=llm, mcp_client=mcp)
    result = runtime.run(_payload("knowledge_qa"))
    assert result.status == "completed"
    assert "RRC" in result.output["summary"]
    assert all(t.status == "completed" for t in result.tool_calls)


# --------------------------------------------------------------------------- #
# Backwards-compat: legacy callers (no llm/mcp injection) still work.
# --------------------------------------------------------------------------- #


def test_runtime_constructs_without_optional_deps() -> None:
    """``LangGraphRuntime()`` with no kwargs must still build — the
    legacy ``tests/test_langgraph_runtime.py`` suite depends on this
    default-injection shape."""
    runtime = LangGraphRuntime()
    assert runtime.graph is not None


def test_build_langgraph_runtime_singleton_has_default_deps() -> None:
    """``build_langgraph_runtime()`` returns a singleton runtime with
    sensible default dependencies so ``RUNTIME_BACKEND=langgraph``
    still works without any extra wiring."""
    runtime = build_langgraph_runtime()
    assert runtime.graph is not None
