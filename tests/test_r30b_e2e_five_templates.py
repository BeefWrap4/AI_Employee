"""R30-B item 4/6: end-to-end 5-template LangGraph coverage.

Spec §5.5 mandates the 5-template set (knowledge_qa, rca, inspection,
change_assessment, ticket_summary).  R30-B item 6 verifies every
template can drive the LangGraph runtime end-to-end and produce a
real, attributed result:

* the run resolves a non-None ``prompt_version`` + ``model_name``
  (R30-B §6.4 attribution),
* the ``tool_calls`` list matches the template's declared
  ``tool_names`` (in order),
* read-only templates (knowledge_qa / inspection / ticket_summary)
  complete with executed tools, approval-required templates (rca /
  change_assessment) pause at ``waiting_approval`` with planned tools,
* the RunStarted NodeTrace carries the model_name + prompt_version.

The MCP client is faked so no network is involved; the LLM client is
faked so the model_name is deterministic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import LangGraphRuntime
from ai_employee.agent_platform_api.runtime import TEMPLATES
from ai_employee.agent_platform_api.schemas import AgentRunCreate
from ai_employee.llm_gateway.client import ChatResponse


class FakeLlmClient:
    def __init__(self, *, content: str = "draft", model: str = "qwen-e2e") -> None:
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
        # Return a plausible answer/summary per tool so the ToolPlan
        # node's summary extraction finds a non-empty string.
        return self._results.get(tool_name, {"answer": f"result for {tool_name}"})


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


def _input_for(template_id: str) -> dict[str, Any]:
    if template_id == "knowledge_qa":
        return {"question": "什么是 RRC？"}
    if template_id == "rca":
        return {"incident_id": "inc_001"}
    if template_id == "inspection":
        return {"target": "NE-001", "check_items": ["cpu", "memory"]}
    if template_id == "change_assessment":
        return {
            "change_id": "CR-2026-0618-001",
            "change_type": "parameter",
            "affected_ne_ids": ["NE-001"],
        }
    if template_id == "ticket_summary":
        return {"ticket_id": "T-1001"}
    return {}


@pytest.mark.parametrize("template_id", list(TEMPLATES.keys()))
def test_each_template_runs_end_to_end_via_langgraph(
    template_id: str, _isolated_tool_log: Path
) -> None:
    """Every one of the 5 templates must drive the LangGraph runtime to
    a terminal / pause state with:
      * a non-None prompt_version + model_name on the response,
      * a tool_calls list matching the template's declared tool_names,
      * a RunStarted node trace carrying model_name + prompt_version.
    """
    mcp = FakeMcpGatewayClient()
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(model="qwen-e2e"),
        mcp_client=mcp,
    )
    template = TEMPLATES[template_id]
    result = runtime.run(
        AgentRunCreate(
            template_id=template_id,
            requested_by="alice",
            input=_input_for(template_id),
        )
    )

    # 1. R30-B §6.4 attribution: every run carries a prompt+model pair.
    assert result.prompt_version is not None, template_id
    assert result.model_name == "qwen-e2e"

    # 2. tool_calls list matches the template's declared tool_names.
    declared = list(template.tool_names)
    actual = [t.tool_name for t in result.tool_calls]
    assert actual == declared, template_id

    # 3. RunStarted node trace carries model_name + prompt_version.
    started = next(n for n in result.node_trace if n.node_name == "RunStarted")
    assert started.model_name == "qwen-e2e"
    assert started.prompt_version == result.prompt_version

    # 4. Approval-required → waiting_approval + planned tools; read-only
    #    → completed + executed tools.
    if template.requires_approval:
        assert result.status == "waiting_approval"
        assert result.approval_status == "pending"
        assert all(t.status == "planned" for t in result.tool_calls)
        # HITL gate fires before execution — MCP client not invoked.
        assert mcp.calls == []
    else:
        assert result.status == "completed"
        assert all(t.status == "completed" for t in result.tool_calls)
        # Read-only templates execute every declared tool exactly once.
        # R32-B: the parallel subgraph fan-out makes invocation order
        # non-deterministic, so assert set equality rather than sequence.
        invoked = [name for name, _ in mcp.calls]
        assert sorted(invoked) == sorted(declared)


def test_all_five_templates_have_distinct_prompt_versions(
    _isolated_tool_log: Path,
) -> None:
    """Each template resolves to a distinct prompt_version label so the
    observability join can separate runs by prompt (spec §6.4)."""
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(model="qwen-e2e"),
        mcp_client=FakeMcpGatewayClient(),
    )
    seen: dict[str, str] = {}
    for template_id in TEMPLATES:
        result = runtime.run(
            AgentRunCreate(
                template_id=template_id,
                requested_by="alice",
                input=_input_for(template_id),
            )
        )
        assert result.prompt_version is not None
        seen[template_id] = result.prompt_version
    assert len(set(seen.values())) == len(seen)
    # knowledge_qa mirrors the knowledge-api convention.
    assert seen["knowledge_qa"] == "rag-template-v1"


def test_each_template_output_has_real_summary(
    _isolated_tool_log: Path,
) -> None:
    """The run output for every template must carry a non-empty summary
    reflecting the LLM-drafted content (RunStarted node writes
    ChatResponse.content into output.summary)."""
    runtime = LangGraphRuntime(
        llm_client=FakeLlmClient(content="real LLM answer", model="qwen-e2e"),
        mcp_client=FakeMcpGatewayClient(),
    )
    for template_id in TEMPLATES:
        result = runtime.run(
            AgentRunCreate(
                template_id=template_id,
                requested_by="alice",
                input=_input_for(template_id),
            )
        )
        assert result.output.get("summary"), template_id
        # The LLM content is persisted verbatim into the summary.
        assert "real LLM answer" in result.output["summary"], template_id
