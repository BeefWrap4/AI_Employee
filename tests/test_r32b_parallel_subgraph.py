"""R32-B: LangGraph parallel subgraph orchestration (spec P3 §5.2).

R29-B connected the real node bodies (LLM + MCP) and R31-B added the
MemorySaver checkpointer + :meth:`resume`.  The remaining gap from spec
§5.2 ("支持并行子任务和结果汇总") was the *subgraph*: ToolPlan iterated
``template.tool_names`` linearly, invoking each tool sequentially.  Spec
§5.2 requires parallel subtask execution with result aggregation.

This module pins the new parallel-subgraph contract:

  * ``LANGGRAPH_SUBGRAPH`` (default ``true``) selects the subgraph path;
    ``false`` keeps the legacy linear ToolPlan as a fallback.
  * The ToolPlan step becomes a *dispatcher*: it fans out one
    ``Send("ToolExec", ...)`` per tool via the LangGraph ``Send`` API so
    every tool executes in parallel.
  * A ``tool_results`` state field (``Annotated[list, operator.add]``)
    is the reducer that merges the parallel worker outputs back into the
    graph state.
  * A ``ToolAggregate`` node curates the merged results into the run's
    ``tool_calls`` list and aggregates them into ``run.output`` (e.g.
    ``risk_factors`` for ``change_assessment``).
  * A failed tool is isolated — its worker records a ``failed`` entry
    with an ``error_code``; the other tools still complete.
  * The checkpointer resume contract (R31-B) is preserved: an
    approval-required run still parks at the ApprovalRequired interrupt
    and resumes to completion, with the parallel subgraph executing the
    held tools after approval.

The ``change_assessment`` template (3 tools: ``cmdb.lookup``,
``ticket.history.search``, ``knowledge-api.chat.query``) is the
canonical fixture because it is the only template with >2 tools and is
approval-required — so it exercises both the parallel fan-out *and* the
resume-then-execute path.
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
# Fakes (mirror test_langgraph_checkpoint_resume.py so real node bodies run)
# --------------------------------------------------------------------------- #


class FakeLlmClient:
    """Records every chat invocation; returns a configured response."""

    def __init__(
        self, *, content: str = "LLM drafted an answer.", model: str = "fake-model"
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


class _RecordingMcp:
    """MCP fake that records the *order* of invoke_tool calls and can
    inject per-tool errors / latencies.

    ``invoke_tool`` sleeps for ``delay`` seconds when configured so tests
    can assert that tools actually ran in parallel (overlap) rather than
    sequentially.
    """

    def __init__(
        self,
        *,
        results: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, str] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self._results = results or {}
        self._errors = errors or {}
        self._delays = delays or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # ``start_ts`` / ``end_ts`` per tool name let the parallelism
        # test assert the execution windows overlap.
        self.start_ts: dict[str, float] = {}
        self.end_ts: dict[str, float] = {}

    def invoke_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        import time

        self.calls.append((tool_name, arguments))
        self.start_ts[tool_name] = time.perf_counter()
        delay = self._delays.get(tool_name, 0.0)
        if delay:
            time.sleep(delay)
        self.end_ts[tool_name] = time.perf_counter()
        if tool_name in self._errors:
            raise RuntimeError(self._errors[tool_name])
        return self._results.get(tool_name, {"ok": True, "tool": tool_name})


def _change_payload() -> AgentRunCreate:
    return AgentRunCreate(
        template_id="change_assessment",
        requested_by="alice",
        input={"change_id": "chg_001", "change_type": "parameter", "affected_ne_ids": ["ne-1"]},
    )


def _ticket_payload() -> AgentRunCreate:
    """ticket_summary is read-only with 2 tools — exercises the
    parallel subgraph on a read-only template (no approval gate)."""
    return AgentRunCreate(
        template_id="ticket_summary",
        requested_by="alice",
        input={"ticket_id": "tk-1"},
    )


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def _subgraph_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly enable the subgraph path (the default, but pinned so a
    globally-set ``LANGGRAPH_SUBGRAPH=false`` cannot mask regressions)."""
    monkeypatch.setenv("LANGGRAPH_SUBGRAPH", "true")


@pytest.fixture
def _subgraph_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGGRAPH_SUBGRAPH", "false")


# --------------------------------------------------------------------------- #
# 1. ToolPlan executes tools in a parallel subgraph (Send fan-out)
# --------------------------------------------------------------------------- #


def test_tool_plan_executes_tools_in_parallel_subgraph(
    _isolated_tool_log: Path,
    _subgraph_enabled: None,
) -> None:
    """Each tool in ``template.tool_names`` must be invoked exactly once,
    and the three ``change_assessment`` tools must execute in *parallel*
    (overlapping execution windows), not sequentially.

    The ``change_assessment`` template is approval-required, so the tools
    are held ``planned`` at the pause and only execute after the approval
    decision is resumed — the parallel subgraph runs during the resume
    leg.
    """
    mcp = _RecordingMcp(
        delays={
            "cmdb.lookup": 0.15,
            "ticket.history.search": 0.15,
            "knowledge-api.chat.query": 0.15,
        },
        results={
            "cmdb.lookup": {"ne_id": "ne-1", "class": "RNC"},
            "ticket.history.search": {"tickets": []},
            "knowledge-api.chat.query": {"answer": "SOP ref"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    paused = runtime.run(_change_payload())
    assert paused.status == "waiting_approval"
    # Tools are planned (not executed) before the HITL gate.
    assert {t.status for t in paused.tool_calls} == {"planned"}
    assert mcp.calls == []

    resumed = runtime.resume(
        paused.run_id,
        decision="approved",
        decided_by="reviewer",
        comment="LGTM",
    )
    assert resumed.status == "completed"
    # All three tools were invoked exactly once.
    invoked = [name for name, _ in mcp.calls]
    assert sorted(invoked) == sorted(
        ["cmdb.lookup", "ticket.history.search", "knowledge-api.chat.query"]
    )
    # Parallelism: the execution windows must overlap. Sequential
    # execution of three 0.15s tools would take >= 0.45s; parallel
    # execution overlaps so the total wall time is ~0.15s. Assert via
    # overlap: tool A's start is before tool C's end AND they overlap.
    starts = [mcp.start_ts[n] for n in invoked]
    ends = [mcp.end_ts[n] for n in invoked]
    # If sequential, each tool would finish before the next starts; i.e.
    # max(starts) > min(ends) is the overlap signature.
    assert max(starts) < max(ends)
    assert min(ends) > min(starts)
    # Stronger: at least two tools overlap (the latest start is before
    # the earliest end among the *other* tools).
    sorted_ends = sorted(ends)
    sorted_starts = sorted(starts)
    # The 2nd-latest start must begin before the earliest end for true
    # overlap (parallel), otherwise it is sequential.
    assert sorted_starts[1] < sorted_ends[0], "tools did not execute in parallel"


# --------------------------------------------------------------------------- #
# 2. Subgraph aggregates results to run.output
# --------------------------------------------------------------------------- #


def test_subgraph_aggregates_results_to_output(
    _isolated_tool_log: Path,
    _subgraph_enabled: None,
) -> None:
    """The parallel subgraph's per-tool results must be aggregated into
    ``run.output`` so downstream consumers see the merged picture, not
    just the LLM summary.

    For ``change_assessment`` the aggregated output carries
    ``risk_factors`` populated from the three tool results.
    """
    mcp = _RecordingMcp(
        results={
            "cmdb.lookup": {"ne_id": "ne-1", "class": "RNC", "criticality": "high"},
            "ticket.history.search": {"tickets": [{"id": "t1", "severity": "P1"}]},
            "knowledge-api.chat.query": {"answer": "SOP-42: verify impact window"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    paused = runtime.run(_change_payload())
    resumed = runtime.resume(
        paused.run_id,
        decision="approved",
        decided_by="reviewer",
        comment=None,
    )
    assert resumed.status == "completed"
    # tool_calls reflects all three tools, completed, with their results.
    assert len(resumed.tool_calls) == 3
    assert {t.status for t in resumed.tool_calls} == {"completed"}
    # The aggregated output carries the per-tool results.
    assert "tool_results" in resumed.output
    tool_names_in_output = {r["tool_name"] for r in resumed.output["tool_results"]}
    assert tool_names_in_output == {
        "cmdb.lookup",
        "ticket.history.search",
        "knowledge-api.chat.query",
    }
    # risk_factors is derived from the aggregated results (a non-empty
    # list), proving the aggregation fed the run output.
    assert resumed.output.get("risk_factors")


# --------------------------------------------------------------------------- #
# 3. Subgraph failure isolates the failed tool
# --------------------------------------------------------------------------- #


def test_subgraph_failure_isolates_failed_tool(
    _isolated_tool_log: Path,
    _subgraph_enabled: None,
) -> None:
    """When one tool raises, only that tool is marked ``failed`` with an
    ``error_code``; the other tools still complete and the run still
    finalises (approved).  The failed tool's result is recorded as an
    error entry in the aggregated output.
    """
    mcp = _RecordingMcp(
        errors={"ticket.history.search": "upstream 503"},
        results={
            "cmdb.lookup": {"ne_id": "ne-1"},
            "knowledge-api.chat.query": {"answer": "SOP ref"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    paused = runtime.run(_change_payload())
    resumed = runtime.resume(
        paused.run_id,
        decision="approved",
        decided_by="reviewer",
        comment=None,
    )
    assert resumed.status == "completed"
    by_name = {t.tool_name: t for t in resumed.tool_calls}
    assert by_name["ticket.history.search"].status == "failed"
    assert by_name["cmdb.lookup"].status == "completed"
    assert by_name["knowledge-api.chat.query"].status == "completed"
    # The aggregated output records the failure (with error_code) so
    # callers can surface it — the ToolCallSummary schema drops the
    # error_code field (pydantic extra=ignore), so the canonical place
    # to assert failure isolation is the aggregated tool_results list
    # and the tool_call_log row.
    err_entries = [
        r
        for r in resumed.output.get("tool_results", [])
        if r.get("tool_name") == "ticket.history.search"
    ]
    assert err_entries
    assert err_entries[0].get("status") == "failed"
    assert err_entries[0].get("error_code") == "tool_invocation_error"
    # The other two tools have successful aggregated entries.
    ok_entries = [
        r
        for r in resumed.output.get("tool_results", [])
        if r.get("tool_name") in ("cmdb.lookup", "knowledge-api.chat.query")
    ]
    assert len(ok_entries) == 2
    assert {e.get("status") for e in ok_entries} == {"completed"}
    # The tool_call_log row for the failed tool carries the error_code.
    from ai_employee.agent_platform_api.tool_call_log import (
        PlatformToolCallLogStore,
    )

    store = PlatformToolCallLogStore()
    rows = store.list_for_run(resumed.run_id)
    failed_rows = [r for r in rows if r["tool_name"] == "ticket.history.search"]
    assert failed_rows
    assert failed_rows[0]["status"] == "failure"
    assert failed_rows[0]["error_code"] == "tool_invocation_error"


# --------------------------------------------------------------------------- #
# 4. Subgraph preserves checkpointer resume (R31-B compatibility)
# --------------------------------------------------------------------------- #


def test_subgraph_preserves_checkpointer_resume(
    _isolated_tool_log: Path,
    _subgraph_enabled: None,
) -> None:
    """The parallel subgraph must not break the R31-B checkpointer
    contract: an approval-required run still parks at the
    ApprovalRequired interrupt, and a *fresh runtime sharing the same
    checkpointer* can resume it to completion — with the parallel
    subgraph executing the held tools after the approval decision.
    """
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    mcp_a = _RecordingMcp(
        results={
            "cmdb.lookup": {"ne_id": "ne-1"},
            "ticket.history.search": {"tickets": []},
            "knowledge-api.chat.query": {"answer": "SOP ref"},
        },
    )
    runtime_a = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=mcp_a,
        checkpointer=saver,
    )
    paused = runtime_a.run(_change_payload())
    assert paused.status == "waiting_approval"
    assert runtime_a.has_checkpoint(paused.run_id)
    assert runtime_a.next_node(paused.run_id) == "ApprovalRequired"

    # A brand-new runtime sharing the same checkpointer resumes the run.
    mcp_b = _RecordingMcp(
        results={
            "cmdb.lookup": {"ne_id": "ne-1"},
            "ticket.history.search": {"tickets": []},
            "knowledge-api.chat.query": {"answer": "SOP ref"},
        },
    )
    runtime_b = LangGraphRuntime(
        llm_client=FakeLlmClient(),
        mcp_client=mcp_b,
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
    # The parallel subgraph executed all three tools during resume.
    assert len(mcp_b.calls) == 3
    assert {t.status for t in resumed.tool_calls} == {"completed"}


# --------------------------------------------------------------------------- #
# 5. Read-only template also uses the parallel subgraph
# --------------------------------------------------------------------------- #


def test_subgraph_runs_for_readonly_template(
    _isolated_tool_log: Path,
    _subgraph_enabled: None,
) -> None:
    """A read-only template (ticket_summary, 2 tools) must also execute
    its tools via the parallel subgraph in a single invoke (no approval
    gate)."""
    mcp = _RecordingMcp(
        delays={"ticket.fetch": 0.1, "knowledge-api.chat.query": 0.1},
        results={
            "ticket.fetch": {"ticket_id": "tk-1", "status": "closed"},
            "knowledge-api.chat.query": {"answer": "root cause ref"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_ticket_payload())
    assert result.status == "completed"
    assert len(mcp.calls) == 2
    starts = [mcp.start_ts[n] for n, _ in mcp.calls]
    ends = [mcp.end_ts[n] for n, _ in mcp.calls]
    sorted_starts = sorted(starts)
    sorted_ends = sorted(ends)
    assert sorted_starts[1] < sorted_ends[0], "tools did not execute in parallel"
    assert {t.status for t in result.tool_calls} == {"completed"}
    assert result.output.get("tool_results")


# --------------------------------------------------------------------------- #
# 6. Fallback: LANGGRAPH_SUBGRAPH=false keeps the legacy linear ToolPlan
# --------------------------------------------------------------------------- #


def test_linear_fallback_preserves_pre_r32_behaviour(
    _isolated_tool_log: Path,
    _subgraph_disabled: None,
) -> None:
    """With ``LANGGRAPH_SUBGRAPH=false`` the legacy linear ToolPlan runs
    (no Send fan-out, no ``tool_results`` aggregation, no post-approval
    tool execution).  This is the pre-R32 escape hatch for environments
    that want the original "tools stay planned, marked completed on
    approval" semantics — tools are *not* invoked and the output is not
    aggregated."""
    mcp = _RecordingMcp(
        results={
            "cmdb.lookup": {"ne_id": "ne-1"},
            "ticket.history.search": {"tickets": []},
            "knowledge-api.chat.query": {"answer": "SOP ref"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    paused = runtime.run(_change_payload())
    resumed = runtime.resume(
        paused.run_id,
        decision="approved",
        decided_by="reviewer",
        comment=None,
    )
    assert resumed.status == "completed"
    # Pre-R32: approval-required tools are never invoked — they move
    # straight from ``planned`` to ``completed`` on approval.
    assert mcp.calls == []
    assert {t.status for t in resumed.tool_calls} == {"completed"}
    # No aggregated tool_results field on the linear path.
    assert "tool_results" not in resumed.output


def test_linear_fallback_readonly_still_invokes_tools(
    _isolated_tool_log: Path,
    _subgraph_disabled: None,
) -> None:
    """The linear fallback still executes tools for *read-only* templates
    (the pre-R32 R29-B behaviour) — the fallback only differs from the
    subgraph path on the approval-required post-approval execution."""
    mcp = _RecordingMcp(
        results={
            "ticket.fetch": {"ticket_id": "tk-1"},
            "knowledge-api.chat.query": {"answer": "ref"},
        },
    )
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=mcp)
    result = runtime.run(_ticket_payload())
    assert result.status == "completed"
    assert len(mcp.calls) == 2
    assert {t.status for t in result.tool_calls} == {"completed"}
    # Linear path does not aggregate tool_results into the output.
    assert "tool_results" not in result.output
