"""LangGraph v1 agent runtime (spec P3 §4 LangGraph v1).

A parallel runtime that drives agent execution through a LangGraph
:class:`StateGraph` instead of the hand-built DAG in ``runtime.py``.
Both runtimes produce the same public :class:`AgentRunResponse` shape
and node-trace semantics so the HTTP layer is agnostic to which is
active.  Selection is via ``RUNTIME_BACKEND`` env:

  ``RUNTIME_BACKEND=langgraph`` → this module
  ``RUNTIME_BACKEND=dag`` (default) → the existing ``runtime.py``

The graph mirrors the DAG's node sequence:

  TemplateLoaded → RunStarted → ToolPlan → (ApprovalRequired | Completed)

For approval-required templates the graph pauses at ``ApprovalRequired``
(returns ``waiting_approval``); :meth:`decide` then appends the
``ApprovalApproved`` / ``ApprovalRejected`` node and finalises the run.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from ai_employee.agent_platform_api.runtime import (
    TEMPLATES,
    _approved_output,
    _output_for_template,
)
from ai_employee.agent_platform_api.schemas import (
    AgentRunCreate,
    AgentRunResponse,
    ApprovalTask,
    NodeTrace,
    ToolCallSummary,
)


class _RunState(TypedDict, total=False):
    """Mutable state carried through the LangGraph nodes."""

    run_id: str
    trace_id: str
    template_id: str
    agent_name: str
    requested_by: str
    input: dict[str, Any]
    output: dict[str, Any]
    status: str
    approval_status: str
    node_trace: list[dict[str, str]]
    tool_calls: list[dict[str, str]]
    requires_approval: bool
    final_node: str


class LangGraphRuntime:
    """Drives agent runs through a LangGraph StateGraph."""

    def __init__(self) -> None:
        self._runs: dict[str, AgentRunResponse] = {}
        self._tasks: dict[str, ApprovalTask] = {}
        self._count = 0
        self._task_count = 0
        self.node_names: set[str] = set()
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # Graph construction
    # ------------------------------------------------------------------ #

    def _build_graph(self) -> Any:
        from langgraph.graph import END, StateGraph

        builder = StateGraph(_RunState)

        builder.add_node("TemplateLoaded", self._node_template_loaded)
        builder.add_node("RunStarted", self._node_run_started)
        builder.add_node("ToolPlan", self._node_tool_plan)
        builder.add_node("ApprovalRequired", self._node_approval_required)
        builder.add_node("Completed", self._node_completed)
        for name in (
            "TemplateLoaded", "RunStarted", "ToolPlan",
            "ApprovalRequired", "Completed",
        ):
            self.node_names.add(name)

        builder.set_entry_point("TemplateLoaded")
        builder.add_edge("TemplateLoaded", "RunStarted")
        builder.add_edge("RunStarted", "ToolPlan")
        builder.add_conditional_edges(
            "ToolPlan",
            self._route_after_tool_plan,
            {"approval": "ApprovalRequired", "done": "Completed"},
        )
        builder.add_edge("ApprovalRequired", END)
        builder.add_edge("Completed", END)
        return builder.compile()

    def _route_after_tool_plan(self, state: _RunState) -> Literal["approval", "done"]:
        return "approval" if state.get("requires_approval") else "done"

    # ------------------------------------------------------------------ #
    # Node implementations
    # ------------------------------------------------------------------ #

    def _node_template_loaded(self, state: _RunState) -> _RunState:
        template = TEMPLATES[state["template_id"]]
        state["agent_name"] = template.agent_name
        state["requires_approval"] = template.requires_approval
        state["node_trace"].append({
            "node_name": "TemplateLoaded",
            "status": "completed",
            "detail": f"Loaded {template.template_id}@{template.version}.",
        })
        return state

    def _node_run_started(self, state: _RunState) -> _RunState:
        state["node_trace"].append({
            "node_name": "RunStarted",
            "status": "completed",
            "detail": f"Run requested by {state['requested_by']}.",
        })
        return state

    def _node_tool_plan(self, state: _RunState) -> _RunState:
        template = TEMPLATES[state["template_id"]]
        state["tool_calls"] = [
            {
                "tool_name": name,
                "risk_level": "approval_required" if state["requires_approval"] else "read_only",
                "status": "planned" if state["requires_approval"] else "completed",
            }
            for name in template.tool_names
        ]
        state["node_trace"].append({
            "node_name": "ToolPlan",
            "status": "completed",
            "detail": f"Planned {len(template.tool_names)} tool calls.",
        })
        return state

    def _node_approval_required(self, state: _RunState) -> _RunState:
        state["status"] = "waiting_approval"
        state["approval_status"] = "pending"
        state["final_node"] = "ApprovalRequired"
        state["node_trace"].append({
            "node_name": "ApprovalRequired",
            "status": "pending",
            "detail": "Human approval required before final write-back.",
        })
        return state

    def _node_completed(self, state: _RunState) -> _RunState:
        state["status"] = "completed"
        state["approval_status"] = "not_required"
        state["final_node"] = "Completed"
        state["node_trace"].append({
            "node_name": "Completed",
            "status": "completed",
            "detail": "Run completed with read-only tools.",
        })
        return state

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self, payload: AgentRunCreate) -> AgentRunResponse:
        template = TEMPLATES[payload.template_id]
        self._count += 1
        run_id = f"lg_run_{self._count:03d}"
        initial: _RunState = {
            "run_id": run_id,
            "trace_id": f"trace_{run_id}",
            "template_id": payload.template_id,
            "agent_name": template.agent_name,
            "requested_by": payload.requested_by,
            "input": payload.input,
            "output": _output_for_template(template.template_id, payload.input),
            "status": "running",
            "approval_status": "not_required",
            "node_trace": [],
            "tool_calls": [],
            "requires_approval": template.requires_approval,
            "final_node": "",
        }
        final_state = self.graph.invoke(initial)
        run = self._to_response(run_id, final_state)
        self._runs[run_id] = run
        if template.requires_approval:
            self._task_count += 1
            task_id = f"lg_task_{self._task_count:03d}"
            task = ApprovalTask(
                task_id=task_id, run_id=run_id, template_id=template.template_id,
                requested_by=payload.requested_by, status="pending",
                risk_level="approval_required",
                reason="Human approval required before final write-back.",
            )
            self._tasks[task_id] = task
        return run

    def pending_approval_task(self, run_id: str) -> ApprovalTask | None:
        for task in self._tasks.values():
            if task.run_id == run_id and task.status == "pending":
                return task
        return None

    def decide(
        self, run_id: str, *, decision: str, decided_by: str, comment: str | None,
    ) -> AgentRunResponse:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        task = self.pending_approval_task(run_id)
        approved = decision == "approved"
        if task is not None:
            self._tasks[task.task_id] = task.model_copy(update={
                "status": decision, "decided_by": decided_by, "comment": comment,
            })
        node_name = "ApprovalApproved" if approved else "ApprovalRejected"
        new_trace = [*run.node_trace, NodeTrace(
            node_name=node_name,
            status="completed" if approved else "failed",
            detail=comment or f"Approval {decision} by {decided_by}.",
        )]
        new_tools = [
            t.model_copy(update={"status": "completed" if approved else "skipped"})
            for t in run.tool_calls
        ]
        updated = run.model_copy(update={
            "status": "completed" if approved else "failed",
            "approval_status": decision,
            "node_trace": new_trace,
            "tool_calls": new_tools,
            "output": _approved_output(run.output, approved),
        })
        self._runs[run_id] = updated
        return updated

    def _to_response(self, run_id: str, state: _RunState) -> AgentRunResponse:
        template = TEMPLATES[state["template_id"]]
        return AgentRunResponse(
            run_id=run_id,
            template_id=state["template_id"],
            agent_name=state["agent_name"],
            status=state["status"],  # type: ignore[arg-type]
            trace_id=state["trace_id"],
            requested_by=state["requested_by"],
            input=state["input"],
            output=state["output"],
            node_trace=[NodeTrace(**n) for n in state["node_trace"]],
            tool_calls=[ToolCallSummary(**t) for t in state["tool_calls"]],
            approval_status=state["approval_status"],  # type: ignore[arg-type]
        )


_runtime: LangGraphRuntime | None = None


def build_langgraph_runtime() -> LangGraphRuntime:
    """Return a process-wide singleton LangGraph runtime."""
    global _runtime
    if _runtime is None:
        _runtime = LangGraphRuntime()
    return _runtime


__all__ = [
    "LangGraphRuntime",
    "build_langgraph_runtime",
]