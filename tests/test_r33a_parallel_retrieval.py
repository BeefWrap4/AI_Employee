"""R33-A3: parallel multi-source retrieval via Send (spec P3 §4 LangGraph v1 depth).

R32-B added the parallel ``ToolExec`` subgraph so a template's declared
tools run concurrently.  But knowledge_qa still retrieved from a *single*
knowledge source — one ``knowledge-api.chat.query`` call.  Spec §4 calls
for parallel multi-source retrieval: when a knowledge_qa run declares
multiple knowledge scopes (the template's ``knowledge_scopes`` input),
fan out one ``KnowledgeRetrieve`` worker per source via the LangGraph
``Send`` API, merge the results via an ``Annotated[list, operator.add]``
reducer, and aggregate them in a ``KnowledgeAggregate`` node before the
run finalises.

This module pins the new parallel-retrieval contract:

  * ``LANGGRAPH_PARALLEL_RETRIEVAL`` (default ``false``) selects the
    parallel multi-source path; ``false`` keeps the single-call behaviour
    R32-B/R29-B established.
  * With the env on, a knowledge_qa run that declares N scopes fans out
    N ``Send("KnowledgeRetrieve", {scope})`` workers — one per source.
  * A ``retrieval_results`` state field (``Annotated[list, operator.add]``)
    is the reducer that merges the parallel worker outputs back into the
    graph state.
  * A ``KnowledgeAggregate`` node distils the merged results into the run
    output (``citations`` / ``sources``) so downstream consumers see the
    multi-source picture.
  * With the env off (the default), the existing single-call path is
    unchanged — knowledge_qa completes in one invoke with no
    ``KnowledgeRetrieve`` / ``KnowledgeAggregate`` nodes in the trace.

Default-off guarantees the existing 1674 tests are unaffected.
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


class FakeLlmClient:
    def __init__(
        self, *, content: str = "LLM drafted an answer.", model: str = "fake-model"
    ) -> None:
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


class _RecordingMcp:
    """MCP fake that records every invoke_tool call and can vary the
    answer by the requested scope (so the parallel workers produce
    distinguishable results)."""

    def __init__(
        self,
        *,
        results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        # Default: echo the scope back in the answer so each worker's
        # result is distinguishable.
        scope = arguments.get("knowledge_scope") or arguments.get("scope") or "default"
        return self._results.get(scope, {"answer": f"answer from {scope}"})


def _multi_scope_payload() -> AgentRunCreate:
    """A knowledge_qa run that declares three knowledge scopes — the
    fixture for the parallel fan-out."""
    return AgentRunCreate(
        template_id="knowledge_qa",
        requested_by="alice",
        input={
            "question": "5G 切换失败原因有哪些？",
            "knowledge_scopes": ["wireless", "transport", "core"],
        },
    )


def _single_scope_payload() -> AgentRunCreate:
    """A knowledge_qa run with a single scope — exercises the
    single-worker fan-out (N=1)."""
    return AgentRunCreate(
        template_id="knowledge_qa",
        requested_by="alice",
        input={
            "question": "什么是 RRC？",
            "knowledge_scopes": ["wireless"],
        },
    )


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def _parallel_retrieval_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_PARALLEL_RETRIEVAL", "true")


@pytest.fixture
def _parallel_retrieval_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_PARALLEL_RETRIEVAL", "false")


# --------------------------------------------------------------------------- #
# 1. Parallel retrieval fans out one worker per declared scope
# --------------------------------------------------------------------------- #


def test_parallel_retrieval_fans_out_one_worker_per_scope(
    _isolated_tool_log: Path,
    _parallel_retrieval_on: None,
) -> None:
    """With ``LANGGRAPH_PARALLEL_RETRIEVAL=true`` a knowledge_qa run that
    declares three scopes must fan out three ``KnowledgeRetrieve`` workers
    — one per scope — and record a tool_call_log row per worker."""
    mcp = _RecordingMcp()
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_multi_scope_payload())
    assert result.status == "completed"
    # Three workers ran — one per declared scope.
    assert len(mcp.calls) == 3
    invoked_scopes = {(args.get("knowledge_scope") or args.get("scope")) for _, args in mcp.calls}
    assert invoked_scopes == {"wireless", "transport", "core"}
    node_names = [n.node_name for n in result.node_trace]
    assert "KnowledgeAggregate" in node_names


# --------------------------------------------------------------------------- #
# 2. Parallel retrieval aggregates results into run.output
# --------------------------------------------------------------------------- #


def test_parallel_retrieval_aggregates_results(
    _isolated_tool_log: Path,
    _parallel_retrieval_on: None,
) -> None:
    """The merged per-source results must be aggregated into
    ``run.output`` so downstream consumers see the multi-source picture
    (a ``sources`` list carrying every scope's answer)."""
    mcp = _RecordingMcp(
        results={
            "wireless": {"answer": "wireless-side HO failure"},
            "transport": {"answer": "transport link degradation"},
            "core": {"answer": "core AMF signalling timeout"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_multi_scope_payload())
    assert result.status == "completed"
    sources = result.output.get("sources")
    assert sources is not None
    assert len(sources) == 3
    source_scopes = {s.get("scope") for s in sources}
    assert source_scopes == {"wireless", "transport", "core"}
    answers = {s.get("answer") for s in sources}
    assert "wireless-side HO failure" in answers
    assert "transport link degradation" in answers


# --------------------------------------------------------------------------- #
# 3. Single-scope run still works (N=1 fan-out)
# --------------------------------------------------------------------------- #


def test_parallel_retrieval_single_scope(
    _isolated_tool_log: Path,
    _parallel_retrieval_on: None,
) -> None:
    """A run with a single declared scope must still fan out (N=1) and
    aggregate — the parallel path is the same shape regardless of N."""
    mcp = _RecordingMcp()
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_single_scope_payload())
    assert result.status == "completed"
    assert len(mcp.calls) == 1
    sources = result.output.get("sources")
    assert sources is not None
    assert len(sources) == 1
    assert sources[0].get("scope") == "wireless"


# --------------------------------------------------------------------------- #
# 4. Default-off: existing single-call path is unchanged
# --------------------------------------------------------------------------- #


def test_default_off_single_call_path_unchanged(
    _isolated_tool_log: Path,
    _parallel_retrieval_off: None,
) -> None:
    """With ``LANGGRAPH_PARALLEL_RETRIEVAL=false`` (the default) the
    existing single-call path must run — knowledge_qa invokes the single
    ``knowledge-api.chat.query`` tool via the normal ToolPlan path, no
    KnowledgeAggregate node, no ``sources`` aggregation."""
    mcp = _RecordingMcp()
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_multi_scope_payload())
    assert result.status == "completed"
    node_names = [n.node_name for n in result.node_trace]
    assert "KnowledgeAggregate" not in node_names
    # The default-off path does NOT fan out per-scope workers.
    assert "sources" not in result.output


def test_default_off_uses_single_tool_call(
    _isolated_tool_log: Path,
    _parallel_retrieval_off: None,
) -> None:
    """Default-off: exactly one knowledge-api.chat.query invocation via
    the normal ToolPlan / ToolExec subgraph path (the pre-R33 behaviour)."""
    mcp = _RecordingMcp()
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_single_scope_payload())
    assert result.status == "completed"
    # Single tool invocation through the normal path.
    assert len(mcp.calls) == 1
    assert mcp.calls[0][0] == "knowledge-api.chat.query"
