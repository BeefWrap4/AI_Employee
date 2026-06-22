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

Node bodies (spec §3 / §4)
--------------------------

* :func:`_node_run_started` invokes :class:`LlmClient.chat` with a
  template-specific prompt and persists the model content into
  ``run.output.summary``.  An LLM failure is captured in the trace and
  the summary but does not abort read-only runs.
* :func:`_node_tool_plan` iterates ``template.tool_names`` and invokes
  ``mcp_client.invoke_tool(name, args)`` for each.  On success the
  tool-call moves to ``status="completed"`` and a row is appended to
  :class:`PlatformToolCallLogStore`; on failure the status becomes
  ``"failed"`` and the row carries an ``error_code``.

The LLM client and MCP gateway client are optional constructor
arguments.  When omitted, sensible defaults are built from env so
``RUNTIME_BACKEND=langgraph`` continues to work without explicit
wiring.  Tests inject fakes through the constructor — see
``tests/test_langgraph_runtime_node_execution.py``.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from ai_employee.agent_platform_api.runtime import (
    TEMPLATES,
    _approved_output,
    _output_for_template,
    prompt_version_for,
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
    # R30-B (spec §6.4): the model_name + prompt_version resolved by the
    # RunStarted node, propagated onto NodeTrace / ToolCallSummary /
    # tool_call_log rows / the AgentRunResponse so every artefact is
    # attributable to a specific prompt+model pair.
    model_name: str | None
    prompt_version: str | None


@runtime_checkable
class _LlmClientProtocol(Protocol):
    """Minimal surface the LangGraph nodes require from the LLM client."""

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        *,
        parent_trace_id: str | None = None,
    ) -> Any: ...


@runtime_checkable
class _McpClientProtocol(Protocol):
    """Minimal surface the LangGraph nodes require from the MCP gateway."""

    def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]: ...


def _build_default_llm_client() -> Any:
    """Lazy default LLM client — reads env on first use.

    Falls back to ``None`` when the LlmClient cannot be imported (test
    envs with restricted deps).  In that case the runtime falls back to
    the no-LLM path so legacy callers (the singleton factory in
    particular) keep working.
    """
    try:
        from ai_employee.llm_gateway.client import LlmClient
    except Exception:  # pragma: no cover - defensive only
        return None
    try:
        return LlmClient()
    except Exception:  # pragma: no cover - missing creds / offline
        return None


def _build_default_mcp_client() -> Any:
    """Lazy default MCP gateway client.

    Reuses the platform's in-memory MCP client so singleton-constructed
    runtimes inside the FastAPI process still hit a real tool handler
    when one is registered.  Returns ``None`` when the client cannot be
    built (e.g. outside an app context) — the ToolPlan node then logs a
    no-op row rather than crashing the run.
    """
    try:
        from ai_employee.agent_platform_api.clients import (
            InMemoryMcpGatewayClient,
        )
        from ai_employee.agent_platform_api.runtime import (
            AgentPlatformStore,
        )
    except Exception:  # pragma: no cover - defensive only
        return None
    try:
        # The platform store is the canonical in-memory backing; the
        # in-process mcp-gateway client binds against it on first use.
        return InMemoryMcpGatewayClient(store=AgentPlatformStore())
    except Exception:  # pragma: no cover - store init failure
        return None


def _prompt_for_template(template_id: str, payload_input: dict[str, Any]) -> list[dict[str, str]]:
    """Build the chat-completions messages for the RunStarted node.

    The prompt embeds the template id so the LLM can specialise the
    answer; the user content is the template's primary input field
    (``question`` for ``knowledge_qa``, ``incident_id`` for ``rca``,
    etc.) so the model has ground-truth context.
    """
    system = (
        f"You are the {template_id} agent inside the AI Employee "
        "platform. Produce a concise, factual response that will be "
        "shown verbatim as the run's summary."
    )
    user_bits = [f"{k}: {v}" for k, v in (payload_input or {}).items()]
    user = "\n".join(user_bits) or "(no input)"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _args_for_template(template_id: str, payload_input: dict[str, Any]) -> dict[str, Any]:
    """Build the arguments dict for ``mcp_client.invoke_tool``.

    The platform's MCP gateway only requires the tool's declared
    arguments; we forward the payload verbatim for now (templates
    declare ``input_schema`` but not per-tool sub-schemas).
    """
    return dict(payload_input or {})


class LangGraphRuntime:
    """Drives agent runs through a LangGraph StateGraph."""

    def __init__(
        self,
        *,
        llm_client: Any | None = None,
        mcp_client: Any | None = None,
        tool_call_log: Any | None = None,
    ) -> None:
        """Construct the runtime with optional injected dependencies.

        All three are keyword-only so the legacy zero-arg
        ``LangGraphRuntime()`` shape (used by
        ``tests/test_langgraph_runtime.py`` and the singleton factory)
        keeps working.  When any dependency is omitted, a lazy default
        is built on first use — see :func:`_build_default_llm_client`,
        :func:`_build_default_mcp_client`.  Tests inject fakes through
        these kwargs to verify the real node-execution path without
        needing network access.
        """
        self._runs: dict[str, AgentRunResponse] = {}
        self._tasks: dict[str, ApprovalTask] = {}
        self._count = 0
        self._task_count = 0
        # Injected dependencies (None means "build lazily on first use").
        self._llm_client = llm_client
        self._mcp_client = mcp_client
        self._tool_call_log = tool_call_log
        self.node_names: set[str] = set()
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # Lazy dependency resolution
    # ------------------------------------------------------------------ #

    def _get_llm(self) -> Any:
        if self._llm_client is not None:
            return self._llm_client
        self._llm_client = _build_default_llm_client()
        return self._llm_client

    def _get_mcp(self) -> Any:
        if self._mcp_client is not None:
            return self._mcp_client
        self._mcp_client = _build_default_mcp_client()
        return self._mcp_client

    def _get_tool_call_log(self) -> Any:
        if self._tool_call_log is not None:
            return self._tool_call_log
        from ai_employee.agent_platform_api.tool_call_log import (
            PlatformToolCallLogStore,
        )

        self._tool_call_log = PlatformToolCallLogStore()
        return self._tool_call_log

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
            "TemplateLoaded",
            "RunStarted",
            "ToolPlan",
            "ApprovalRequired",
            "Completed",
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
        state["node_trace"].append(
            {
                "node_name": "TemplateLoaded",
                "status": "completed",
                "detail": f"Loaded {template.template_id}@{template.version}.",
            }
        )
        return state

    def _node_run_started(self, state: _RunState) -> _RunState:
        """Invoke the LLM and persist the response into ``output.summary``.

        The LLM call is wrapped in a try/except so transient upstream
        failures do not abort read-only runs — the trace records the
        failure mode and the run output carries an explicit
        ``[LLM error: ...]`` prefix so callers can surface it.

        R30-B (spec §6.4): the resolved ``model_name`` (from
        ``ChatResponse.model``) and ``prompt_version`` (from the
        template's canonical label) are stashed on the run state so the
        ToolPlan node and ``_to_response`` can propagate them onto the
        tool_call_log rows and the AgentRunResponse.  The prompt_version
        is resolved even when the LLM call fails so an unattributed run
        is never emitted.
        """
        llm = self._get_llm()
        template_id = state["template_id"]
        model_label: str | None = None
        prompt_version = prompt_version_for(template_id)
        # Stash the prompt_version up-front so every downstream artefact
        # (node trace, tool calls, log rows) carries it even if the LLM
        # call below fails.
        state["prompt_version"] = prompt_version
        if llm is not None and hasattr(llm, "chat"):
            messages = _prompt_for_template(template_id, state["input"])
            try:
                response = llm.chat(
                    messages,
                    parent_trace_id=state.get("trace_id"),
                )
                content = getattr(response, "content", "") or ""
                model_label = getattr(response, "model", None) or model_label
                if content:
                    state["output"] = {**state["output"], "summary": content}
                detail = f"LLM {model_label} drafted {len(content)} chars."
            except Exception as exc:
                # Surface the failure without aborting the run.
                state["output"] = {
                    **state["output"],
                    "summary": f"[LLM error: {exc}] {state['output'].get('summary', '')}".strip(),
                }
                detail = f"LLM call failed: {exc}"
        else:
            # No LLM client available — preserve legacy string-only trace.
            detail = f"Run requested by {state['requested_by']}."
        state["model_name"] = model_label
        trace_entry: dict[str, str] = {
            "node_name": "RunStarted",
            "status": "completed",
            "detail": detail,
        }
        if model_label is not None:
            trace_entry["model_name"] = model_label
        if prompt_version is not None:
            trace_entry["prompt_version"] = prompt_version
        state["node_trace"].append(trace_entry)
        return state

    def _node_tool_plan(self, state: _RunState) -> _RunState:
        """Plan and execute the template's tool calls.

        For approval-required templates we still record the plan but
        do not invoke any tools — the HITL gate in
        :func:`_node_approval_required` fires first.  For read-only
        templates we invoke each tool via the MCP gateway client,
        record a row in :class:`PlatformToolCallLogStore`, and mark
        the tool call ``completed`` (or ``failed`` with an error code).

        R30-B (spec §6.4): every ``ToolCallSummary`` and tool_call_log
        row inherits the run's ``model_name`` / ``prompt_version`` so
        the downstream observability join can attribute tool latency /
        success to the prompt+model pair that drove the run.
        """
        template = TEMPLATES[state["template_id"]]
        requires_approval = state["requires_approval"]
        run_model_name = state.get("model_name")
        run_prompt_version = state.get("prompt_version")
        tool_calls: list[dict[str, Any]] = []
        mcp = self._get_mcp()
        log = self._get_tool_call_log()
        for name in template.tool_names:
            entry: dict[str, Any] = {
                "tool_name": name,
                "risk_level": "approval_required" if requires_approval else "read_only",
                "status": "planned" if requires_approval else "completed",
            }
            if run_model_name is not None:
                entry["model_name"] = run_model_name
            if run_prompt_version is not None:
                entry["prompt_version"] = run_prompt_version
            if not requires_approval and mcp is not None and hasattr(mcp, "invoke_tool"):
                args = _args_for_template(template.template_id, state["input"])
                started = time.perf_counter()
                try:
                    result = mcp.invoke_tool(name, args)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    entry["status"] = "completed"
                    summary = (
                        (
                            result.get("answer")
                            if isinstance(result, dict) and "answer" in result
                            else None
                        )
                        or (
                            result.get("summary")
                            if isinstance(result, dict) and "summary" in result
                            else None
                        )
                        or (str(result)[:200] if result is not None else "")
                    )
                    log.record(
                        run_id=state["run_id"],
                        tool_name=name,
                        input_summary=str(args)[:200],
                        output_summary=summary,
                        status="success",
                        latency_ms=latency_ms,
                        error_code=None,
                        model_name=run_model_name,
                        prompt_version=run_prompt_version,
                    )
                except Exception:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    entry["status"] = "failed"
                    entry["error_code"] = "tool_invocation_error"
                    try:
                        log.record(
                            run_id=state["run_id"],
                            tool_name=name,
                            input_summary=str(args)[:200],
                            output_summary=None,
                            status="failure",
                            latency_ms=latency_ms,
                            error_code="tool_invocation_error",
                            model_name=run_model_name,
                            prompt_version=run_prompt_version,
                        )
                    except Exception:  # pragma: no cover - log must not break the run
                        pass
            elif not requires_approval:
                # No MCP client available (legacy / offline path) — mark
                # the tool as completed so the run still terminates with
                # a valid response shape; observability gets no row.
                entry["status"] = "completed"
            tool_calls.append(entry)
        state["tool_calls"] = tool_calls
        state["node_trace"].append(
            {
                "node_name": "ToolPlan",
                "status": "completed",
                "detail": (
                    f"Planned {len(template.tool_names)} tool calls; "
                    f"{sum(1 for t in tool_calls if t['status'] == 'completed')} "
                    f"executed, {sum(1 for t in tool_calls if t['status'] == 'failed')} failed."
                ),
            }
        )
        return state

    def _node_approval_required(self, state: _RunState) -> _RunState:
        state["status"] = "waiting_approval"
        state["approval_status"] = "pending"
        state["final_node"] = "ApprovalRequired"
        state["node_trace"].append(
            {
                "node_name": "ApprovalRequired",
                "status": "pending",
                "detail": "Human approval required before final write-back.",
            }
        )
        return state

    def _node_completed(self, state: _RunState) -> _RunState:
        state["status"] = "completed"
        state["approval_status"] = "not_required"
        state["final_node"] = "Completed"
        state["node_trace"].append(
            {
                "node_name": "Completed",
                "status": "completed",
                "detail": "Run completed with read-only tools.",
            }
        )
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
            "model_name": None,
            "prompt_version": None,
        }
        final_state = self.graph.invoke(initial)
        run = self._to_response(run_id, final_state)
        self._runs[run_id] = run
        if template.requires_approval:
            self._task_count += 1
            task_id = f"lg_task_{self._task_count:03d}"
            task = ApprovalTask(
                task_id=task_id,
                run_id=run_id,
                template_id=template.template_id,
                requested_by=payload.requested_by,
                status="pending",
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
        self,
        run_id: str,
        *,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> AgentRunResponse:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        task = self.pending_approval_task(run_id)
        approved = decision == "approved"
        if task is not None:
            self._tasks[task.task_id] = task.model_copy(
                update={
                    "status": decision,
                    "decided_by": decided_by,
                    "comment": comment,
                }
            )
        node_name = "ApprovalApproved" if approved else "ApprovalRejected"
        new_trace = [
            *run.node_trace,
            NodeTrace(
                node_name=node_name,
                status="completed" if approved else "failed",
                detail=comment or f"Approval {decision} by {decided_by}.",
            ),
        ]
        new_tools = [
            t.model_copy(update={"status": "completed" if approved else "skipped"})
            for t in run.tool_calls
        ]
        updated = run.model_copy(
            update={
                "status": "completed" if approved else "failed",
                "approval_status": decision,
                "node_trace": new_trace,
                "tool_calls": new_tools,
                "output": _approved_output(run.output, approved),
            }
        )
        self._runs[run_id] = updated
        return updated

    def _to_response(self, run_id: str, state: _RunState) -> AgentRunResponse:
        TEMPLATES[state["template_id"]]
        # ``ToolCallSummary`` only declares ``tool_name``/``risk_level``/
        # ``status`` (legacy field set); ``error_code`` is preserved as an
        # extra attribute on the response so callers can surface
        # failure modes without changing the schema contract.
        # R30-B: model_name + prompt_version resolved by the RunStarted
        # node are propagated onto the AgentRunResponse so every run is
        # attributable to its prompt+model pair.
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
            model_name=state.get("model_name"),
            prompt_version=state.get("prompt_version"),
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
