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

R31-B (spec §3 / §4 "可恢复"): the graph is compiled with a
:class:`MemorySaver` checkpointer and ``interrupt_before=["ApprovalRequired"]``
so an approval-required run *pauses* at the HITL gate rather than
finalising.  The thread state is persisted under ``thread_id = run_id``;
:meth:`resume` injects the decision via ``graph.update_state`` and then
calls ``graph.invoke(None, config)`` so the graph engine itself drives
the run through ``ApprovalRequired → (ApprovalApproved |
ApprovalRejected) → END``.  This replaces the pre-R31 ``decide`` path
that bypassed the graph engine with a ``model_copy`` stitch (R24 audit
G6).  The legacy :meth:`decide` is retained as a fallback for the
``RUNTIME_BACKEND=langgraph`` + checkpointer-failure path.

For approval-required templates the graph pauses at ``ApprovalRequired``
(returns ``waiting_approval``); :meth:`decide` (legacy fallback) or
:meth:`resume` (checkpointer path) then drives the run to completion.

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

import operator
import os
import time
from typing import Annotated, Any, Literal, Protocol, TypedDict, runtime_checkable

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
    # R33-A2 (spec §4 multi-gate HITL): a second interrupt gate.  When the
    # supplement route is enabled (``LANGGRAPH_SUPPLEMENT_GATE=true``) the
    # run pauses at ``SupplementRequired`` to request supplemental info
    # from the operator; ``resume_from_supplement`` injects the response
    # here and drives the graph forward.  Default-off keeps existing
    # templates on their pre-R33 path.
    requires_supplement: bool
    supplement_response: str | None
    # R30-B (spec §6.4): the model_name + prompt_version resolved by the
    # RunStarted node, propagated onto NodeTrace / ToolCallSummary /
    # tool_call_log rows / the AgentRunResponse so every artefact is
    # attributable to a specific prompt+model pair.
    model_name: str | None
    prompt_version: str | None
    # R32-B (spec §5.2 "并行子任务和结果汇总"): per-tool results emitted
    # by the parallel ``ToolExec`` subgraph workers.  The
    # ``operator.add`` reducer merges the fan-out outputs back into the
    # graph state so the ``ToolAggregate`` node sees every tool's result
    # regardless of completion order.  Only the subgraph path populates
    # this field; the linear fallback (``LANGGRAPH_SUBGRAPH=false``) does
    # not, preserving the pre-R32 output contract.
    tool_results: Annotated[list[dict[str, Any]], operator.add]
    # R33-A3 (spec §4 parallel multi-source retrieval): per-source results
    # emitted by the parallel ``KnowledgeRetrieve`` workers.  When the
    # parallel retrieval path is active (``LANGGRAPH_PARALLEL_RETRIEVAL``)
    # a knowledge_qa run fans out one worker per declared knowledge scope;
    # the ``operator.add`` reducer merges them back so
    # ``KnowledgeAggregate`` sees every source's result.  Only the
    # parallel path populates this field; the default-off single-call
    # path does not, preserving the pre-R33 output contract.
    retrieval_results: Annotated[list[dict[str, Any]], operator.add]


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


def _subgraph_enabled() -> bool:
    """True when the R32-B parallel subgraph path is active.

    Spec §5.2 requires parallel subtask execution with result
    aggregation.  The subgraph (LangGraph ``Send`` fan-out → parallel
    ``ToolExec`` workers → ``ToolAggregate`` reduce) is the default; set
    ``LANGGRAPH_SUBGRAPH=false`` to fall back to the pre-R32 linear
    ToolPlan (the escape hatch for environments that want the original
    "tools stay planned, marked completed on approval" semantics for
    approval-required templates, and the sequential per-tool loop for
    read-only templates).
    """
    return os.getenv("LANGGRAPH_SUBGRAPH", "true").lower() not in ("false", "0", "no")


def _supplement_gate_enabled() -> bool:
    """True when the R33-A2 supplement interrupt gate is active.

    Spec §4 calls for a richer HITL surface where a run can pause to
    request supplemental information from the operator, then resume with
    the response.  The route into ``SupplementRequired`` is gated behind
    ``LANGGRAPH_SUPPLEMENT_GATE`` (default ``false``) so existing
    templates keep their pre-R33 single-gate path.  Turning it on makes
    ``RunStarted`` route a knowledge_qa run to ``SupplementRequired``
    before ToolPlan; the run parks there (when ``SupplementRequired`` is
    in ``LANGGRAPH_INTERRUPT_NODES``) and :meth:`resume_from_supplement`
    drives it forward.
    """
    return os.getenv("LANGGRAPH_SUPPLEMENT_GATE", "false").lower() in ("true", "1", "yes")


def _interrupt_nodes() -> list[str]:
    """The list of node names the graph interrupts *before*.

    R33-A2 (spec §4 multi-gate): ``LANGGRAPH_INTERRUPT_NODES`` (default
    ``"ApprovalRequired"``) is a comma-separated list.  Production
    deployments add ``SupplementRequired`` to enable the second HITL
    gate; the default keeps the pre-R33 single-gate behaviour so every
    existing test that asserts ``interrupt_before`` only contains
    ApprovalRequired continues to pass.
    """
    raw = os.getenv("LANGGRAPH_INTERRUPT_NODES", "ApprovalRequired")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    # Preserve registration order; unknown names are dropped at compile
    # time (the graph only knows the nodes it registered).
    return names or ["ApprovalRequired"]


def _parallel_retrieval_enabled() -> bool:
    """True when the R33-A3 parallel multi-source retrieval path is active.

    Spec §4 calls for parallel multi-source retrieval: a knowledge_qa run
    that declares multiple knowledge scopes fans out one
    ``KnowledgeRetrieve`` worker per source via the LangGraph ``Send``
    API, merges the results via the ``retrieval_results`` reducer, and
    aggregates them in ``KnowledgeAggregate``.  The path is gated behind
    ``LANGGRAPH_PARALLEL_RETRIEVAL`` (default ``false``) so the pre-R33
    single-call knowledge_qa behaviour is preserved.
    """
    return os.getenv("LANGGRAPH_PARALLEL_RETRIEVAL", "false").lower() in (
        "true",
        "1",
        "yes",
    )


def _knowledge_sources(payload_input: dict[str, Any]) -> list[str]:
    """Resolve the knowledge scopes a knowledge_qa run retrieves from.

    The knowledge_qa template declares ``knowledge_scopes`` (an array of
    strings) on its input schema.  When the operator supplies one or more
    scopes, each becomes a parallel retrieval worker; when none are
    supplied we fall back to a single ``"default"`` scope so the fan-out
    still has one worker (the parallel path is the same shape for N=1).
    """
    raw = (payload_input or {}).get("knowledge_scopes")
    if isinstance(raw, list) and raw:
        return [str(s) for s in raw if s]
    return ["default"]


def build_checkpointer() -> Any:
    """Build a LangGraph checkpointer from the ``CHECKPOINTER_BACKEND`` env.

    R33-A1 (spec P3 §4 LangGraph v1 depth): production deployments need
    to swap the R31-B in-process ``MemorySaver`` for a durable backend
    (``RedisSaver`` / ``PostgresSaver``) so a run parked at the HITL gate
    survives a replica restart and can be resumed by another replica.
    This factory reads ``CHECKPOINTER_BACKEND`` and constructs the right
    saver:

      * ``memory`` (default) → :class:`langgraph.checkpoint.memory.MemorySaver`
      * ``redis``  → ``langgraph.checkpoint.redis.RedisSaver``
      * ``postgres`` → ``langgraph.checkpoint.postgres.PostgresSaver``

    The redis / postgres backends live in optional extras
    (``langgraph-checkpoint-redis`` / ``langgraph-checkpoint-postgres``).
    When the requested extra is not installed — or an unknown backend
    value is supplied — the factory degrades to ``MemorySaver`` with a
    warning so the runtime always stays resumable out of the box.

    The redis/postgres savers require an async connection at
    construction; to keep the factory synchronous and dependency-light we
    build them lazily via their ``.from_conn_string``-style constructor
    when available, otherwise fall back to ``MemorySaver``.  The memory
    path is always available and is the backward-compatible default.
    """
    import warnings

    backend = os.getenv("CHECKPOINTER_BACKEND", "memory").strip().lower()

    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    if backend == "redis":
        try:
            from langgraph.checkpoint.redis import RedisSaver
        except Exception as exc:  # pragma: no cover - extra not installed
            warnings.warn(
                "CHECKPOINTER_BACKEND=redis but langgraph-checkpoint-redis is "
                f"not importable ({exc!r}); falling back to MemorySaver.",
                stacklevel=2,
            )
            from langgraph.checkpoint.memory import MemorySaver

            return MemorySaver()
        return _construct_remote_saver(RedisSaver, "redis")

    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except Exception as exc:  # pragma: no cover - extra not installed
            warnings.warn(
                "CHECKPOINTER_BACKEND=postgres but langgraph-checkpoint-postgres "
                f"is not importable ({exc!r}); falling back to MemorySaver.",
                stacklevel=2,
            )
            from langgraph.checkpoint.memory import MemorySaver

            return MemorySaver()
        return _construct_remote_saver(PostgresSaver, "postgres")

    # Unknown backend — degrade to MemorySaver rather than crash.
    warnings.warn(
        f"Unknown CHECKPOINTER_BACKEND={backend!r}; falling back to MemorySaver.",
        stacklevel=2,
    )
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


def _construct_remote_saver(saver_cls: Any, label: str) -> Any:
    """Construct a remote (redis/postgres) saver from its env DSN.

    The langgraph redis/postgres savers ship several constructor shapes
    across versions (``RedisSaver.from_conn_string(...)``,
    ``PostgresSaver(conn)``).  We try the lightweight
    ``from_conn_string`` factory first (passing the conventional DSN env
    var), then a zero-arg construction, and finally fall back to
    ``MemorySaver`` if neither works — a misconfigured DSN must never
    make the runtime non-resumable.
    """
    import warnings

    dsn_env = {
        "redis": "REDIS_CHECKPOINT_URL",
        "postgres": "POSTGRES_CHECKPOINT_URL",
    }.get(label, "")
    dsn = os.getenv(dsn_env) if dsn_env else None
    from_factory = getattr(saver_cls, "from_conn_string", None)
    if callable(from_factory) and dsn:
        try:
            return from_factory(dsn)
        except Exception as exc:  # pragma: no cover - dsn / version specific
            warnings.warn(
                f"{saver_cls.__name__}.from_conn_string failed ({exc!r}); "
                "falling back to MemorySaver.",
                stacklevel=2,
            )
            from langgraph.checkpoint.memory import MemorySaver

            return MemorySaver()
    # No DSN / no factory: fall back to MemorySaver.  We do not attempt a
    # bare ``saver_cls()`` because the remote savers require a live
    # connection object at construction time.
    warnings.warn(
        f"CHECKPOINTER_BACKEND={label} but no {dsn_env} configured; falling back to MemorySaver.",
        stacklevel=2,
    )
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


class LangGraphRuntime:
    """Drives agent runs through a LangGraph StateGraph."""

    def __init__(
        self,
        *,
        llm_client: Any | None = None,
        mcp_client: Any | None = None,
        tool_call_log: Any | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        """Construct the runtime with optional injected dependencies.

        All four are keyword-only so the legacy zero-arg
        ``LangGraphRuntime()`` shape (used by
        ``tests/test_langgraph_runtime.py`` and the singleton factory)
        keeps working.  When any dependency is omitted, a lazy default
        is built on first use — see :func:`_build_default_llm_client`,
        :func:`_build_default_mcp_client`.  Tests inject fakes through
        these kwargs to verify the real node-execution path without
        needing network access.

        R31-B: ``checkpointer`` defaults to a fresh
        :class:`MemorySaver` so every runtime is resumable out of the
        box.  Callers that want cross-runtime durability (e.g. a test
        asserting state survives a runtime swap) pass a shared
        ``MemorySaver`` instance explicitly.
        """
        self._runs: dict[str, AgentRunResponse] = {}
        self._tasks: dict[str, ApprovalTask] = {}
        self._count = 0
        self._task_count = 0
        # Injected dependencies (None means "build lazily on first use").
        self._llm_client = llm_client
        self._mcp_client = mcp_client
        self._tool_call_log = tool_call_log
        self._checkpointer = checkpointer
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
        # R32-B (spec §5.2): the parallel subgraph workers + reducer.
        # ``ToolExec`` runs once per tool (fanned out via ``Send`` from
        # ToolPlan / ApprovalApproved); ``ToolAggregate`` runs once after
        # all workers complete to curate ``tool_calls`` and aggregate the
        # results into ``run.output``.  They are always registered so the
        # graph topology is stable whether the subgraph path is taken or
        # not — the linear fallback simply never routes into them.
        builder.add_node("ToolExec", self._node_tool_exec)
        builder.add_node("ToolAggregate", self._node_tool_aggregate)
        builder.add_node("ApprovalRequired", self._node_approval_required)
        builder.add_node("ApprovalApproved", self._node_approval_approved)
        builder.add_node("ApprovalRejected", self._node_approval_rejected)
        # R33-A2 (spec §4 multi-gate HITL): a second interrupt gate.  The
        # node is always registered so the graph topology is stable; the
        # route into it is gated behind ``LANGGRAPH_SUPPLEMENT_GATE``
        # (default off) so existing templates never see it.
        builder.add_node("SupplementRequired", self._node_supplement_required)
        # R33-A3 (spec §4 parallel multi-source retrieval): the parallel
        # retrieval workers + aggregator.  ``KnowledgeRetrieve`` runs once
        # per declared knowledge scope (fanned out via ``Send`` from
        # RunStarted); ``KnowledgeAggregate`` runs once after all workers
        # complete to distil the per-source results into ``run.output``.
        # Always registered so the topology is stable; the route in is
        # gated behind ``LANGGRAPH_PARALLEL_RETRIEVAL`` (default off).
        builder.add_node("KnowledgeRetrieve", self._node_knowledge_retrieve)
        builder.add_node("KnowledgeAggregate", self._node_knowledge_aggregate)
        builder.add_node("Completed", self._node_completed)
        for name in (
            "TemplateLoaded",
            "RunStarted",
            "ToolPlan",
            "ToolExec",
            "ToolAggregate",
            "ApprovalRequired",
            "ApprovalApproved",
            "ApprovalRejected",
            "SupplementRequired",
            "KnowledgeRetrieve",
            "KnowledgeAggregate",
            "Completed",
        ):
            self.node_names.add(name)

        builder.set_entry_point("TemplateLoaded")
        builder.add_edge("TemplateLoaded", "RunStarted")
        # R33-A2/A3: RunStarted routes to the supplement gate (when
        # enabled and flagged), the parallel retrieval fan-out (when
        # ``LANGGRAPH_PARALLEL_RETRIEVAL`` is on for knowledge_qa), or
        # straight to ToolPlan (the pre-R33 path).  Default-off means the
        # conditional always returns ``toolplan`` unless an env is on.
        builder.add_conditional_edges(
            "RunStarted",
            self._route_after_run_started,
            {
                "supplement": "SupplementRequired",
                "toolplan": "ToolPlan",
                "parallel_retrieval": "KnowledgeRetrieve",
            },
        )
        # After the operator supplies the supplement response the run
        # continues into ToolPlan (then the normal read-only / approval
        # path).
        builder.add_edge("SupplementRequired", "ToolPlan")
        # R33-A3: the parallel retrieval workers all reduce into
        # KnowledgeAggregate, which finalises the run (the retrieval IS
        # the work for knowledge_qa on the parallel path — no separate
        # ToolPlan leg).
        builder.add_edge("KnowledgeRetrieve", "KnowledgeAggregate")
        builder.add_edge("KnowledgeAggregate", "Completed")
        # ToolPlan either fans out to the parallel ToolExec workers
        # (read-only templates, subgraph path) or routes straight to the
        # approval gate / completion (approval-required templates hold
        # their tools ``planned`` until the resume leg executes them).
        builder.add_conditional_edges(
            "ToolPlan",
            self._route_after_tool_plan,
            {
                "approval": "ApprovalRequired",
                "done": "Completed",
                "fanout": "ToolExec",
            },
        )
        # The parallel ToolExec workers all reduce into ToolAggregate,
        # which then routes to the approval gate (read-only leg),
        # completion (read-only leg with no approval), or straight to
        # END (post-approval leg — the run is already finalised by
        # ApprovalApproved and must not be overwritten by Completed).
        builder.add_edge("ToolExec", "ToolAggregate")
        builder.add_conditional_edges(
            "ToolAggregate",
            self._route_after_tool_aggregate,
            {"approval": "ApprovalRequired", "done": "Completed", "end": END},
        )
        # R31-B: after the ApprovalRequired node runs (during resume),
        # the conditional edge routes to ApprovalApproved or
        # ApprovalRejected based on the decision injected via
        # ``graph.update_state`` before the resume invoke.
        builder.add_conditional_edges(
            "ApprovalRequired",
            self._route_after_approval,
            {"approve": "ApprovalApproved", "reject": "ApprovalRejected"},
        )
        # R32-B: ApprovalApproved may fan out the parallel subgraph to
        # execute the held tools (subgraph path) or go straight to END
        # (linear fallback marks the held tools completed without
        # invoking them).
        builder.add_conditional_edges(
            "ApprovalApproved",
            self._route_after_approval_approved,
            {"fanout": "ToolExec", "end": END},
        )
        builder.add_edge("ApprovalRejected", END)
        builder.add_edge("Completed", END)
        # R31-B (spec §3 / §4 "可恢复"): compile with a MemorySaver
        # checkpointer and pause before the HITL gate so the run can be
        # resumed after the approval decision.  The checkpointer persists
        # the thread under ``thread_id = run_id``; production deployments
        # swap in RedisSaver / PostgresSaver for cross-replica durability.
        checkpointer = self._get_checkpointer()
        # R33-A2: the interrupt-before list is configurable via
        # ``LANGGRAPH_INTERRUPT_NODES`` (default ``ApprovalRequired``).
        # Filter to only the nodes this graph registered so an unknown
        # name in the env never crashes compilation.
        interrupt_before = [n for n in _interrupt_nodes() if n in self.node_names] or [
            "ApprovalRequired"
        ]
        return builder.compile(
            checkpointer=checkpointer,
            interrupt_before=interrupt_before,
        )

    def _get_checkpointer(self) -> Any:
        """Return the checkpointer for this runtime.

        Defaults to a factory-built saver (see :func:`build_checkpointer`)
        when none was injected so every runtime is resumable out of the
        box and production deployments can swap in ``RedisSaver`` /
        ``PostgresSaver`` via ``CHECKPOINTER_BACKEND``.  Tests that need
        cross-runtime durability pass a shared ``MemorySaver`` via the
        ``checkpointer`` constructor kwarg (the injected instance always
        wins over the env).
        """
        if self._checkpointer is not None:
            return self._checkpointer
        self._checkpointer = build_checkpointer()
        return self._checkpointer

    def _route_after_run_started(
        self, state: _RunState
    ) -> Literal["supplement", "toolplan", "parallel_retrieval"] | list[Any]:
        """Route out of RunStarted.

        R33-A2 (spec §4 multi-gate HITL): when the supplement gate is
        enabled (``LANGGRAPH_SUPPLEMENT_GATE=true``) and the run is
        flagged ``requires_supplement``, route to ``SupplementRequired``
        so the run parks to request supplemental info from the operator.

        R33-A3 (spec §4 parallel multi-source retrieval): when the
        parallel retrieval path is enabled
        (``LANGGRAPH_PARALLEL_RETRIEVAL=true``) for a knowledge_qa run,
        fan out one ``Send("KnowledgeRetrieve", {scope})`` per declared
        knowledge scope so each source is retrieved in parallel.

        Otherwise (the default) route straight to ``ToolPlan`` — the
        pre-R33 single-gate, single-call path every existing test
        assumes.  The supplement gate takes priority over parallel
        retrieval (an operator may supplement before retrieval runs).
        """
        if _supplement_gate_enabled() and state.get("requires_supplement"):
            return "supplement"
        if _parallel_retrieval_enabled() and state["template_id"] == "knowledge_qa":
            scopes = _knowledge_sources(state.get("input", {}))
            from langgraph.types import Send

            return [
                Send(
                    "KnowledgeRetrieve",
                    {
                        "scope": scope,
                        "template_id": state["template_id"],
                        "input": state.get("input", {}),
                        "run_id": state.get("run_id", ""),
                        "model_name": state.get("model_name"),
                        "prompt_version": state.get("prompt_version"),
                    },
                )
                for scope in scopes
            ]
        return "toolplan"

    def _route_after_tool_plan(
        self, state: _RunState
    ) -> Literal["approval", "done", "fanout"] | list[Any]:
        """Route out of ToolPlan.

        * ``approval`` — approval-required template; tools are held
          ``planned`` at the HITL gate (the resume leg executes them).
        * ``fanout`` — read-only template on the subgraph path; fan out
          one ``Send("ToolExec", ...)`` per tool so they execute in
          parallel.
        * ``done`` — read-only template on the linear fallback path
          (``LANGGRAPH_SUBGRAPH=false``); the linear ToolPlan already
          invoked each tool sequentially and there is nothing left to
          aggregate, so go straight to ``Completed``.
        """
        if state.get("requires_approval"):
            return "approval"
        if not _subgraph_enabled():
            return "done"
        # Subgraph path: fan out one ToolExec Send per tool.  When the
        # template declares no tools the fan-out is empty — route to
        # ``done`` so the graph still terminates without invoking the
        # aggregate node on an empty reducer.
        template = TEMPLATES[state["template_id"]]
        if not template.tool_names:
            return "done"
        from langgraph.types import Send

        return [
            Send(
                "ToolExec",
                {
                    "tool_name": name,
                    "template_id": state["template_id"],
                    "input": state.get("input", {}),
                    "run_id": state.get("run_id", ""),
                    "model_name": state.get("model_name"),
                    "prompt_version": state.get("prompt_version"),
                },
            )
            for name in template.tool_names
        ]

    def _route_after_tool_aggregate(self, state: _RunState) -> Literal["approval", "done", "end"]:
        """After the parallel subgraph aggregates, route to:

        * ``end`` — post-approval leg (``approval_status == "approved"``):
          the run is already finalised by ApprovalApproved; go straight
          to END so the ``Completed`` node does not overwrite the
          approval outcome.
        * ``approval`` — read-only leg that somehow has
          ``requires_approval`` (defensive; the read-only leg normally
          has it false) — park at the HITL gate.
        * ``done`` — read-only leg — finalise via ``Completed``.
        """
        if state.get("approval_status") == "approved":
            return "end"
        return "approval" if state.get("requires_approval") else "done"

    def _route_after_approval_approved(
        self, state: _RunState
    ) -> Literal["fanout", "end"] | list[Any]:
        """After an approved run, either fan out the parallel subgraph
        to execute the held tools (subgraph path) or go straight to END
        (linear fallback marks them completed without invoking)."""
        if not _subgraph_enabled():
            return "end"
        template = TEMPLATES[state["template_id"]]
        if not template.tool_names:
            return "end"
        from langgraph.types import Send

        return [
            Send(
                "ToolExec",
                {
                    "tool_name": name,
                    "template_id": state["template_id"],
                    "input": state.get("input", {}),
                    "run_id": state.get("run_id", ""),
                    "model_name": state.get("model_name"),
                    "prompt_version": state.get("prompt_version"),
                },
            )
            for name in template.tool_names
        ]

    def _route_after_approval(self, state: _RunState) -> Literal["approve", "reject"]:
        """Route to the approval outcome node based on the decision.

        The decision is injected onto the checkpoint state via
        :meth:`resume` (``graph.update_state``) *before* the resume
        invoke, so by the time the ApprovalRequired node runs and this
        edge fires, ``approval_status`` already holds ``approved`` or
        ``rejected``.  The ApprovalRequired node is careful not to
        overwrite an already-decided status.
        """
        if state.get("approval_status") == "approved":
            return "approve"
        return "reject"

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
        """Plan the template's tool calls.

        R32-B (spec §5.2): on the subgraph path this node *only* plans —
        it seeds ``tool_calls`` with one ``planned`` entry per tool and
        records the ToolPlan trace.  The actual execution is fanned out
        to the parallel ``ToolExec`` workers (read-only templates here,
        approval-required templates on the resume leg via
        ApprovalApproved).  ``ToolAggregate`` curates the entries to
        ``completed`` / ``failed`` once the workers finish.

        On the linear fallback path (``LANGGRAPH_SUBGRAPH=false``) this
        node keeps the pre-R32 R29-B behaviour: iterate ``tool_names``
        sequentially, invoke each tool, record a tool_call_log row, and
        mark the entry ``completed`` / ``failed`` in place.  Approval-
        required templates still hold their tools ``planned`` (executed
        neither here nor on resume — the linear fallback's
        ApprovalApproved just marks them ``completed``).

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

        if _subgraph_enabled():
            # Subgraph path: plan only.  Execution is fanned out to the
            # parallel ToolExec workers; ToolAggregate finalises the
            # entries.  Approval-required templates hold ``planned``
            # until the resume leg.
            for name in template.tool_names:
                entry: dict[str, Any] = {
                    "tool_name": name,
                    "risk_level": "approval_required" if requires_approval else "read_only",
                    "status": "planned",
                }
                if run_model_name is not None:
                    entry["model_name"] = run_model_name
                if run_prompt_version is not None:
                    entry["prompt_version"] = run_prompt_version
                tool_calls.append(entry)
            state["tool_calls"] = tool_calls
            state["node_trace"].append(
                {
                    "node_name": "ToolPlan",
                    "status": "completed",
                    "detail": (
                        f"Planned {len(template.tool_names)} tool calls for "
                        f"parallel subgraph execution."
                    ),
                }
            )
            return state

        # --- Linear fallback (pre-R32 R29-B behaviour) ----------------- #
        mcp = self._get_mcp()
        log = self._get_tool_call_log()
        for name in template.tool_names:
            entry = {
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

    # ------------------------------------------------------------------ #
    # R32-B parallel subgraph workers + aggregator
    # ------------------------------------------------------------------ #

    def _node_tool_exec(self, state: dict[str, Any]) -> dict[str, Any]:
        """Execute a single tool (one parallel subgraph worker).

        Invoked once per ``Send`` fanned out from ToolPlan (read-only)
        or ApprovalApproved (approval-required, post-resume).  The
        worker invokes the MCP gateway, records a tool_call_log row, and
        returns a ``tool_results`` entry that the ``operator.add``
        reducer merges back into the graph state.  Failures are isolated
        — a raised exception becomes a ``failed`` entry with an
        ``error_code`` so the other parallel tools still complete.
        """
        name = state["tool_name"]
        template_id = state.get("template_id") or self._current_template_id(state.get("run_id", ""))
        run_id = state.get("run_id", "")
        run_model_name = state.get("model_name")
        run_prompt_version = state.get("prompt_version")
        payload_input = state.get("input", {})
        mcp = self._get_mcp()
        log = self._get_tool_call_log()
        result_entry: dict[str, Any] = {
            "tool_name": name,
            "risk_level": "read_only",
            "status": "completed",
        }
        if run_model_name is not None:
            result_entry["model_name"] = run_model_name
        if run_prompt_version is not None:
            result_entry["prompt_version"] = run_prompt_version
        if mcp is None or not hasattr(mcp, "invoke_tool"):
            # No MCP client — mark completed (legacy offline shape).
            result_entry["result"] = None
            result_entry["summary"] = ""
            return {"tool_results": [result_entry]}
        args = _args_for_template(template_id, payload_input)
        started = time.perf_counter()
        try:
            result = mcp.invoke_tool(name, args)
            latency_ms = int((time.perf_counter() - started) * 1000)
            result_entry["status"] = "completed"
            summary = (
                (result.get("answer") if isinstance(result, dict) and "answer" in result else None)
                or (
                    result.get("summary")
                    if isinstance(result, dict) and "summary" in result
                    else None
                )
                or (str(result)[:200] if result is not None else "")
            )
            result_entry["result"] = result
            result_entry["summary"] = summary
            try:
                log.record(
                    run_id=run_id,
                    tool_name=name,
                    input_summary=str(args)[:200],
                    output_summary=summary,
                    status="success",
                    latency_ms=latency_ms,
                    error_code=None,
                    model_name=run_model_name,
                    prompt_version=run_prompt_version,
                )
            except Exception:  # pragma: no cover - log must not break the run
                pass
        except Exception:
            latency_ms = int((time.perf_counter() - started) * 1000)
            result_entry["status"] = "failed"
            result_entry["error_code"] = "tool_invocation_error"
            result_entry["result"] = None
            result_entry["summary"] = ""
            try:
                log.record(
                    run_id=run_id,
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
        return {"tool_results": [result_entry]}

    def _current_template_id(self, run_id: str) -> str:
        """Defensive fallback for the template_id when a ``Send`` payload
        omits it.

        The fanned-out ``ToolExec`` workers always carry ``template_id``
        in their ``Send`` payload (see :func:`_route_after_tool_plan` /
        :func:`_route_after_approval_approved`), so this is only reached
        if a future caller constructs a worker by hand.  It resolves the
        id from the persisted run response, falling back to
        ``change_assessment`` (the canonical multi-tool template) so the
        args builder never raises.
        """
        run = self._runs.get(run_id)
        if run is not None:
            return run.template_id
        return "change_assessment"

    def _node_tool_aggregate(self, state: _RunState) -> _RunState:
        """Curate the parallel subgraph results into ``tool_calls`` and
        aggregate them into ``run.output``.

        Runs once after every ``ToolExec`` worker has reduced its result
        into ``state["tool_results"]``.  The planned entries seeded by
        ToolPlan are merged with the worker results (matched by
        ``tool_name``) so the final ``tool_calls`` list carries the
        executed status / error_code.  The raw results are also surfaced
        on ``run.output["tool_results"]`` and, for ``change_assessment``,
        distilled into ``risk_factors`` so downstream consumers see the
        aggregated picture.
        """
        results = list(state.get("tool_results", []))
        results_by_name = {r.get("tool_name"): r for r in results}
        # Merge the worker results onto the planned entries, preserving
        # the template's tool order.
        merged: list[dict[str, Any]] = []
        for entry in state.get("tool_calls", []):
            name = entry.get("tool_name")
            worker = results_by_name.get(name)
            if worker is None:
                merged.append(entry)
                continue
            merged_entry = dict(entry)
            merged_entry["status"] = worker.get("status", entry.get("status"))
            if worker.get("error_code"):
                merged_entry["error_code"] = worker["error_code"]
            merged.append(merged_entry)
        state["tool_calls"] = merged
        # Surface the aggregated raw results on the run output.
        output = dict(state.get("output", {}))
        output["tool_results"] = results
        # Template-specific distillation: change_assessment derives
        # ``risk_factors`` from the aggregated tool results so the run
        # output carries an actionable risk signal, not just the LLM
        # summary.
        if state["template_id"] == "change_assessment":
            risk_factors: list[str] = []
            for r in results:
                if r.get("status") == "failed":
                    risk_factors.append(f"{r.get('tool_name')}: invocation failed")
                    continue
                res = r.get("result")
                if isinstance(res, dict):
                    if res.get("criticality") in ("high", "critical"):
                        risk_factors.append(f"cmdb: high-criticality NE ({res.get('ne_id')})")
                    if res.get("tickets"):
                        risk_factors.append(f"tickets: {len(res['tickets'])} historical hits")
                    if res.get("answer"):
                        risk_factors.append(f"kb: {str(res['answer'])[:80]}")
            if risk_factors:
                output["risk_factors"] = risk_factors
                output["risk_level"] = "elevated" if len(risk_factors) > 1 else "low"
        state["output"] = output
        state["node_trace"].append(
            {
                "node_name": "ToolAggregate",
                "status": "completed",
                "detail": (
                    f"Aggregated {len(results)} parallel tool results; "
                    f"{sum(1 for r in results if r.get('status') == 'completed')} "
                    f"completed, {sum(1 for r in results if r.get('status') == 'failed')} failed."
                ),
            }
        )
        return state

    # ------------------------------------------------------------------ #
    # R33-A3 parallel multi-source retrieval workers + aggregator
    # ------------------------------------------------------------------ #

    def _node_knowledge_retrieve(self, state: dict[str, Any]) -> dict[str, Any]:
        """Retrieve from a single knowledge source (one parallel worker).

        Invoked once per ``Send`` fanned out from RunStarted when the
        parallel retrieval path is active.  The worker invokes the
        ``knowledge-api.chat.query`` MCP tool scoped to its declared
        ``scope`` (forwarded as ``knowledge_scope`` so the gateway can
        filter the knowledge base), records a tool_call_log row, and
        returns a ``retrieval_results`` entry that the ``operator.add``
        reducer merges back into the graph state.  Failures are isolated
        — a raised exception becomes a ``failed`` entry so the other
        parallel sources still complete.
        """
        scope = state.get("scope", "default")
        run_id = state.get("run_id", "")
        run_model_name = state.get("model_name")
        run_prompt_version = state.get("prompt_version")
        payload_input = state.get("input", {})
        tool_name = "knowledge-api.chat.query"
        # Forward the scope to the gateway so the knowledge base can be
        # filtered per source; the question is the template's primary
        # input field.
        question = payload_input.get("question", "") if isinstance(payload_input, dict) else ""
        args = {"question": question, "knowledge_scope": scope}
        mcp = self._get_mcp()
        log = self._get_tool_call_log()
        entry: dict[str, Any] = {
            "scope": scope,
            "tool_name": tool_name,
            "status": "completed",
        }
        if run_model_name is not None:
            entry["model_name"] = run_model_name
        if run_prompt_version is not None:
            entry["prompt_version"] = run_prompt_version
        if mcp is None or not hasattr(mcp, "invoke_tool"):
            entry["answer"] = None
            entry["summary"] = ""
            return {"retrieval_results": [entry]}
        started = time.perf_counter()
        try:
            result = mcp.invoke_tool(tool_name, args)
            latency_ms = int((time.perf_counter() - started) * 1000)
            entry["status"] = "completed"
            answer = (
                (result.get("answer") if isinstance(result, dict) and "answer" in result else None)
                or (
                    result.get("summary")
                    if isinstance(result, dict) and "summary" in result
                    else None
                )
                or (str(result)[:200] if result is not None else "")
            )
            entry["answer"] = answer
            entry["summary"] = answer
            try:
                log.record(
                    run_id=run_id,
                    tool_name=tool_name,
                    input_summary=str(args)[:200],
                    output_summary=answer,
                    status="success",
                    latency_ms=latency_ms,
                    error_code=None,
                    model_name=run_model_name,
                    prompt_version=run_prompt_version,
                )
            except Exception:  # pragma: no cover - log must not break the run
                pass
        except Exception:
            latency_ms = int((time.perf_counter() - started) * 1000)
            entry["status"] = "failed"
            entry["error_code"] = "tool_invocation_error"
            entry["answer"] = None
            entry["summary"] = ""
            try:
                log.record(
                    run_id=run_id,
                    tool_name=tool_name,
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
        return {"retrieval_results": [entry]}

    def _node_knowledge_aggregate(self, state: _RunState) -> _RunState:
        """Aggregate the parallel multi-source retrieval results.

        Runs once after every ``KnowledgeRetrieve`` worker has reduced
        its result into ``state["retrieval_results"]``.  Distils the
        per-source answers into a ``sources`` list on ``run.output`` so
        downstream consumers see the multi-source picture, and seeds a
        ``tool_calls`` summary entry per source so the public response
        shape still carries the tool-call ledger.
        """
        results = list(state.get("retrieval_results", []))
        sources: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        run_model_name = state.get("model_name")
        run_prompt_version = state.get("prompt_version")
        for r in results:
            scope = r.get("scope", "default")
            sources.append(
                {
                    "scope": scope,
                    "answer": r.get("answer"),
                    "status": r.get("status", "completed"),
                }
            )
            entry: dict[str, Any] = {
                "tool_name": r.get("tool_name", "knowledge-api.chat.query"),
                "risk_level": "read_only",
                "status": r.get("status", "completed"),
            }
            if run_model_name is not None:
                entry["model_name"] = run_model_name
            if run_prompt_version is not None:
                entry["prompt_version"] = run_prompt_version
            if r.get("error_code"):
                entry["error_code"] = r["error_code"]
            tool_calls.append(entry)
        state["tool_calls"] = tool_calls
        output = dict(state.get("output", {}))
        output["sources"] = sources
        # If a summary was not yet drafted by the LLM, synthesise one
        # from the aggregated answers so the run output always carries a
        # top-level summary.
        if not output.get("summary"):
            joined = "; ".join(
                f"{s['scope']}: {s.get('answer')}" for s in sources if s.get("answer")
            )
            if joined:
                output["summary"] = joined
        state["output"] = output
        state["node_trace"].append(
            {
                "node_name": "KnowledgeAggregate",
                "status": "completed",
                "detail": (
                    f"Aggregated {len(results)} parallel retrieval results; "
                    f"{sum(1 for r in results if r.get('status') == 'completed')} "
                    f"completed, {sum(1 for r in results if r.get('status') == 'failed')} failed."
                ),
            }
        )
        return state

    def _node_approval_required(self, state: _RunState) -> _RunState:
        """Record the HITL pause.

        R31-B: this node runs during *resume* (the run was parked at
        ``interrupt_before=["ApprovalRequired"]`` after the first
        invoke).  It must not overwrite a decision that
        :meth:`resume` already injected via ``graph.update_state`` —
        otherwise the conditional edge below would always see ``pending``
        and route to reject.  We only seed ``approval_status="pending"``
        when no decision has been injected yet (defensive — the normal
        resume path always injects before resuming).
        """
        state["status"] = "waiting_approval"
        if state.get("approval_status") in (None, ""):
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

    def _node_supplement_required(self, state: _RunState) -> _RunState:
        """Park the run to request supplemental information from the operator.

        R33-A2 (spec §4 multi-gate HITL): this is the second interrupt
        gate.  The node runs during the *initial* invoke (the run is
        parked at ``interrupt_before=["SupplementRequired"]``); it sets a
        ``supplement_pending`` status and records the HITL trace entry so
        the public contract surfaces that the run is waiting on the
        operator.  :meth:`resume_from_supplement` injects the operator's
        response and drives the graph forward into ToolPlan.
        """
        state["status"] = "supplement_pending"
        state["final_node"] = "SupplementRequired"
        state["node_trace"].append(
            {
                "node_name": "SupplementRequired",
                "status": "pending",
                "detail": "Supplemental information requested from operator.",
            }
        )
        return state

    def _node_approval_approved(self, state: _RunState) -> _RunState:
        """Finalise an approved run (driven by the graph engine on resume).

        R32-B: on the subgraph path this node sets the approval outcome
        but leaves the held tool calls ``planned`` — the conditional
        edge fans them out to the parallel ``ToolExec`` workers, and
        ``ToolAggregate`` finalises their status.  On the linear
        fallback path (``LANGGRAPH_SUBGRAPH=false``) this node preserves
        the pre-R32 behaviour: mark the held tools ``completed``
        without invoking them.
        """
        state["status"] = "completed"
        state["approval_status"] = "approved"
        state["final_node"] = "ApprovalApproved"
        state["output"] = _approved_output(state.get("output", {}), approved=True)
        state["node_trace"].append(
            {
                "node_name": "ApprovalApproved",
                "status": "completed",
                "detail": "Approval approved; run finalised.",
            }
        )
        if _subgraph_enabled():
            # Subgraph path: leave the held tools ``planned`` — the
            # parallel ToolExec workers (fanned out by
            # ``_route_after_approval_approved``) execute them and
            # ``ToolAggregate`` finalises the status.  Reset the
            # ``tool_results`` reducer so the resume leg aggregates a
            # clean list (the planning leg never populated it).
            state["tool_results"] = []
            return state
        # Linear fallback: approval-required tool calls were held
        # ``planned`` at the pause; on approval they move to
        # ``completed`` (pre-R32 semantics — not actually invoked).
        state["tool_calls"] = [
            {**t, "status": "completed"} if t.get("status") == "planned" else t
            for t in state.get("tool_calls", [])
        ]
        return state

    def _node_approval_rejected(self, state: _RunState) -> _RunState:
        """Terminate a rejected run (driven by the graph engine on resume)."""
        state["status"] = "failed"
        state["approval_status"] = "rejected"
        state["final_node"] = "ApprovalRejected"
        state["output"] = _approved_output(state.get("output", {}), approved=False)
        state["node_trace"].append(
            {
                "node_name": "ApprovalRejected",
                "status": "failed",
                "detail": "Approval rejected; run terminated.",
            }
        )
        # Rejected tool calls stay ``planned`` (never executed) — mirror
        # the legacy ``decide`` path which marks them ``skipped``.
        state["tool_calls"] = [
            {**t, "status": "skipped"} if t.get("status") == "planned" else t
            for t in state.get("tool_calls", [])
        ]
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
        # R33-A2: a run is flagged ``requires_supplement`` only when the
        # supplement gate is enabled.  The gate is default-off so every
        # existing template keeps its pre-R33 path.  ``knowledge_qa`` is
        # the canonical supplement template (read-only, the operator may
        # supply extra context); when the gate is on it parks at
        # ``SupplementRequired`` before ToolPlan.
        requires_supplement = _supplement_gate_enabled() and payload.template_id == "knowledge_qa"
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
            "requires_supplement": requires_supplement,
            "supplement_response": None,
            "final_node": "",
            "model_name": None,
            "prompt_version": None,
            "tool_results": [],
            "retrieval_results": [],
        }
        config = self._config(run_id)
        final_state = self.graph.invoke(initial, config=config)
        # R31-B: the checkpointer parks an approval-required run *before*
        # the ApprovalRequired node runs, so the post-invoke state still
        # says ``running`` and the node_trace lacks the HITL gate entry.
        # Synthesise the paused-view state so the response matches the
        # pre-R31 ``waiting_approval`` contract while leaving the
        # checkpoint itself parked at the interrupt (``next ==
        # ("ApprovalRequired",)``) for :meth:`resume` to drive forward.
        if self._is_parked_at_approval(config):
            final_state = self._paused_state(final_state)
        # R33-A2: the supplement gate parks a run *before* the
        # SupplementRequired node runs, so the post-invoke state still
        # says ``running`` — synthesise the ``supplement_pending`` view
        # the public contract expects.
        elif self._is_parked_at_supplement(config):
            final_state = self._supplement_paused_state(final_state)
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

    # ------------------------------------------------------------------ #
    # Checkpointer helpers (R31-B)
    # ------------------------------------------------------------------ #

    def _config(self, run_id: str) -> dict[str, Any]:
        """Build the LangGraph config that pins a run to a thread."""
        return {"configurable": {"thread_id": run_id}}

    def _is_parked_at_approval(self, config: dict[str, Any]) -> bool:
        """True when the checkpoint is paused before ApprovalRequired."""
        try:
            snapshot = self.graph.get_state(config)
        except Exception:  # pragma: no cover - defensive
            return False
        nxt = getattr(snapshot, "next", None)
        return bool(nxt) and "ApprovalRequired" in tuple(nxt)

    @staticmethod
    def _paused_state(final_state: _RunState) -> _RunState:
        """Synthesise the paused-view state for a parked approval run.

        The ApprovalRequired node has NOT executed (the run is parked at
        ``interrupt_before``), so we project the ``waiting_approval``
        status and the HITL trace entry the public contract expects —
        without mutating the checkpoint.  The real ApprovalRequired
        node runs during :meth:`resume` and appends its own trace then.
        """
        state: _RunState = dict(final_state)  # shallow copy
        state["status"] = "waiting_approval"
        state["approval_status"] = "pending"
        state["final_node"] = "ApprovalRequired"
        trace = list(state.get("node_trace", []))
        if not any(isinstance(t, dict) and t.get("node_name") == "ApprovalRequired" for t in trace):
            trace.append(
                {
                    "node_name": "ApprovalRequired",
                    "status": "pending",
                    "detail": "Human approval required before final write-back.",
                }
            )
        state["node_trace"] = trace
        return state

    def _is_parked_at_supplement(self, config: dict[str, Any]) -> bool:
        """True when the checkpoint is paused before SupplementRequired.

        R33-A2: the second HITL gate.  Mirrors :meth:`_is_parked_at_approval`
        so the supplement pause view can be synthesised without mutating the
        checkpoint.
        """
        try:
            snapshot = self.graph.get_state(config)
        except Exception:  # pragma: no cover - defensive
            return False
        nxt = getattr(snapshot, "next", None)
        return bool(nxt) and "SupplementRequired" in tuple(nxt)

    @staticmethod
    def _supplement_paused_state(final_state: _RunState) -> _RunState:
        """Synthesise the paused-view state for a parked supplement run.

        The SupplementRequired node has NOT executed (the run is parked at
        ``interrupt_before``), so we project the ``supplement_pending``
        status and the HITL trace entry the public contract expects —
        without mutating the checkpoint.  The real SupplementRequired node
        runs during :meth:`resume_from_supplement`.
        """
        state: _RunState = dict(final_state)  # shallow copy
        state["status"] = "supplement_pending"
        state["final_node"] = "SupplementRequired"
        trace = list(state.get("node_trace", []))
        if not any(
            isinstance(t, dict) and t.get("node_name") == "SupplementRequired" for t in trace
        ):
            trace.append(
                {
                    "node_name": "SupplementRequired",
                    "status": "pending",
                    "detail": "Supplemental information requested from operator.",
                }
            )
        state["node_trace"] = trace
        return state

    def has_checkpoint(self, run_id: str) -> bool:
        """True when a persisted thread exists for ``run_id``."""
        try:
            snapshot = self.graph.get_state(self._config(run_id))
        except Exception:  # pragma: no cover - defensive
            return False
        # A non-persisted thread returns an empty snapshot (no values,
        # no next); treat that as "no checkpoint".
        return bool(getattr(snapshot, "next", None)) or bool(getattr(snapshot, "values", None))

    def next_node(self, run_id: str) -> str | None:
        """Return the next node the parked thread will execute, or None.

        Used by tests to assert the run is parked at the
        ApprovalRequired interrupt (read-only runs return ``None``
        because they completed in one invoke).
        """
        try:
            snapshot = self.graph.get_state(self._config(run_id))
        except Exception:  # pragma: no cover - defensive
            return None
        nxt = getattr(snapshot, "next", None)
        if not nxt:
            return None
        return next(iter(nxt))

    def resume(
        self,
        run_id: str,
        *,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> AgentRunResponse:
        """Resume a parked approval run and drive it to completion.

        R31-B (spec §3 / §4 "可恢复"): the decision is injected onto the
        checkpoint state via ``graph.update_state`` and the graph engine
        is then driven forward with ``graph.invoke(None, config)``.  The
        ApprovalRequired node runs (preserving the injected decision),
        the conditional edge routes to ApprovalApproved /
        ApprovalRejected, and the run finalises — all through the graph
        engine, not a ``model_copy`` stitch.

        Raises :class:`KeyError` when no persisted thread exists for
        ``run_id`` (e.g. an unknown id, or a read-only run that never
        parked).
        """
        config = self._config(run_id)
        if not self.has_checkpoint(run_id):
            raise KeyError(run_id)
        if not self._is_parked_at_approval(config):
            # The run already completed (e.g. read-only) or was already
            # resumed — nothing to drive forward.
            raise KeyError(run_id)
        # Inject the decision so the ApprovalRequired node's conditional
        # edge routes to the right outcome node.
        self.graph.update_state(
            config,
            {
                "approval_status": decision,
                "approval_decided_by": decided_by,
                "approval_comment": comment,
            },
        )
        final_state = self.graph.invoke(None, config=config)
        run = self._to_response(run_id, final_state)
        self._runs[run_id] = run
        # Reflect the decision on the pending approval task, mirroring
        # the legacy ``decide`` path so callers that read the task see
        # the resolved status.
        task = self.pending_approval_task(run_id)
        if task is not None:
            self._tasks[task.task_id] = task.model_copy(
                update={
                    "status": decision,
                    "decided_by": decided_by,
                    "comment": comment,
                }
            )
        return run

    def resume_from_supplement(
        self,
        run_id: str,
        *,
        supplement_response: str,
    ) -> AgentRunResponse:
        """Resume a run parked at the SupplementRequired interrupt.

        R33-A2 (spec §4 multi-gate HITL): the operator's supplemental
        response is injected onto the checkpoint state via
        ``graph.update_state`` and the graph engine is then driven
        forward with ``graph.invoke(None, config)``.  The
        SupplementRequired node runs, the edge routes to ToolPlan, and
        the run continues down its normal read-only / approval path —
        all through the graph engine, mirroring :meth:`resume`.

        The injected response is also surfaced on ``run.output["supplement_response"]``
        so downstream consumers (the LLM prompt, the response contract)
        can join the operator's context to the run.

        Raises :class:`KeyError` when no persisted thread exists for
        ``run_id`` or the thread is not parked at SupplementRequired.
        """
        config = self._config(run_id)
        if not self.has_checkpoint(run_id):
            raise KeyError(run_id)
        if not self._is_parked_at_supplement(config):
            # The run already completed or was already resumed — nothing
            # to drive forward.
            raise KeyError(run_id)
        # Inject the operator's supplement response so the
        # SupplementRequired node + ToolPlan see it, and surface it on the
        # run output so the public contract carries the operator context.
        self.graph.update_state(
            config,
            {"supplement_response": supplement_response},
        )
        final_state = self.graph.invoke(None, config=config)
        # Surface the supplement response on the run output (the graph
        # state holds it but the output dict may not carry it yet).
        output = dict(final_state.get("output", {}))
        output.setdefault("supplement_response", supplement_response)
        final_state["output"] = output
        run = self._to_response(run_id, final_state)
        self._runs[run_id] = run
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
    "build_checkpointer",
    "build_langgraph_runtime",
]
