"""R33-A2: multi-gate supplement interrupt (spec P3 §4 LangGraph v1 depth).

R31-B added a single HITL interrupt at ``ApprovalRequired``.  Spec §4
calls for a richer human-in-the-loop surface where a run can *also*
pause to request supplemental information from the operator (the
runtime already has a ``supplement_pending`` state machine in
``runtime.py``), then resume with the operator's response.  Pre-R33
the LangGraph layer had no second interrupt gate and no
``resume_from_supplement`` path.

This module pins the new multi-gate contract:

  * A ``SupplementRequired`` node sets a ``supplement_pending``-like
    status and parks the run.
  * ``LANGGRAPH_INTERRUPT_NODES`` (default ``"ApprovalRequired"``) is a
    comma-separated list of nodes the graph interrupts *before*.  When
    ``SupplementRequired`` is in the list the graph parks there.
  * ``resume_from_supplement(run_id, supplement_response)`` injects the
    operator's response and drives the run to completion via
    ``graph.invoke(None, config)`` — mirroring :meth:`resume`.
  * The supplement route is gated behind ``LANGGRAPH_SUPPLEMENT_GATE``
    (default ``false``) so existing templates keep their current path.

Default-off guarantees the existing 1670 tests are unaffected.
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
    def __init__(self, results: dict[str, dict[str, Any]] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        return self._results.get(tool_name, {"ok": True, "tool": tool_name})


def _knowledge_payload() -> AgentRunCreate:
    return AgentRunCreate(
        template_id="knowledge_qa",
        requested_by="alice",
        input={"question": "什么是 RRC？"},
    )


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def _supplement_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the supplement gate and add SupplementRequired to the
    interrupt-before list so the graph parks there."""
    monkeypatch.setenv("LANGGRAPH_SUPPLEMENT_GATE", "true")
    monkeypatch.setenv("LANGGRAPH_INTERRUPT_NODES", "ApprovalRequired,SupplementRequired")


# --------------------------------------------------------------------------- #
# 1. With the gate on, a knowledge_qa run parks at SupplementRequired
# --------------------------------------------------------------------------- #


def test_supplement_gate_parks_at_supplement_required(
    _isolated_tool_log: Path,
    _supplement_gate_on: None,
) -> None:
    """With ``LANGGRAPH_SUPPLEMENT_GATE=true`` and ``SupplementRequired``
    in the interrupt list, a knowledge_qa run must pause *before* the
    SupplementRequired node and surface a supplement-pending status."""
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=_RecordingMcp())
    result = runtime.run(_knowledge_payload())
    # Parked, not completed.
    assert result.status == "supplement_pending"
    # The persisted thread is parked at the SupplementRequired interrupt.
    assert runtime.next_node(result.run_id) == "SupplementRequired"
    node_names = [n.node_name for n in result.node_trace]
    assert "SupplementRequired" in node_names
    # Has not reached Completed yet.
    assert "Completed" not in node_names


# --------------------------------------------------------------------------- #
# 2. resume_from_supplement drives the run to completion
# --------------------------------------------------------------------------- #


def test_resume_from_supplement_completes_run(
    _isolated_tool_log: Path,
    _supplement_gate_on: None,
) -> None:
    """``resume_from_supplement`` injects the operator's supplement
    response and drives the graph to completion — the run finalises as
    ``completed`` and the thread is no longer parked."""
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=_RecordingMcp())
    paused = runtime.run(_knowledge_payload())
    assert paused.status == "supplement_pending"

    resumed = runtime.resume_from_supplement(
        paused.run_id,
        supplement_response="补充信息：RRC 是无线资源控制协议。",
    )
    assert resumed.status == "completed"
    # Thread no longer parked.
    assert runtime.next_node(paused.run_id) in (None, "")
    # The supplement response is surfaced on the run output.
    assert resumed.output.get("supplement_response") == "补充信息：RRC 是无线资源控制协议。"


# --------------------------------------------------------------------------- #
# 3. Default-off: existing templates never see SupplementRequired
# --------------------------------------------------------------------------- #


def test_default_off_knowledge_qa_completes_without_supplement(
    _isolated_tool_log: Path,
) -> None:
    """With the gate off (the default), a knowledge_qa run must complete
    in a single invoke and never park at SupplementRequired — the
    existing pre-R33 behaviour is preserved."""
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=_RecordingMcp())
    result = runtime.run(_knowledge_payload())
    assert result.status == "completed"
    node_names = [n.node_name for n in result.node_trace]
    assert "SupplementRequired" not in node_names
    assert "Completed" in node_names
    assert runtime.next_node(result.run_id) in (None, "")


# --------------------------------------------------------------------------- #
# 4. resume_from_supplement on an unknown / non-parked run raises
# --------------------------------------------------------------------------- #


def test_resume_from_supplement_unknown_run_raises(
    _isolated_tool_log: Path,
    _supplement_gate_on: None,
) -> None:
    """Resuming a run id with no persisted checkpoint at SupplementRequired
    raises KeyError — mirroring :meth:`resume`."""
    runtime = LangGraphRuntime(llm_client=FakeLlmClient(), mcp_client=_RecordingMcp())
    with pytest.raises(KeyError):
        runtime.resume_from_supplement("lg_run_missing", supplement_response="x")
