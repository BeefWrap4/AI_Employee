from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ai_employee.agent_platform_api.audit import record_event
from ai_employee.agent_platform_api.schemas import (
    AgentRunCreate,
    AgentRunResponse,
    AgentTemplate,
    ApprovalTask,
    NodeTrace,
    ToolCallSummary,
    ToolRegistration,
    ToolResponse,
)

TEMPLATES: dict[str, AgentTemplate] = {
    "knowledge_qa": AgentTemplate(
        template_id="knowledge_qa",
        agent_name="Knowledge QA Agent",
        version="v1",
        status="published",
        description="Answer telecom operations questions with cited knowledge evidence.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "knowledge_scopes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "citations": {"type": "array"},
            },
        },
        tool_names=["knowledge-api.chat.query"],
        requires_approval=False,
    ),
    "rca": AgentTemplate(
        template_id="rca",
        agent_name="RCA Agent",
        version="v1",
        status="published",
        description="Analyze incident evidence and produce RCA hypotheses for expert review.",
        input_schema={
            "type": "object",
            "properties": {
                "incident_id": {"type": "string"},
                "alarms": {"type": "array"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "hypotheses": {"type": "array"},
            },
        },
        tool_names=["rca-agent.runs.create", "rca-agent.reports.review"],
        requires_approval=True,
    ),
    "inspection": AgentTemplate(
        template_id="inspection",
        agent_name="Inspection Agent",
        version="v1",
        status="published",
        description="Run read-only inspection checks and summarize anomalies.",
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "check_items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["target"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "findings": {"type": "array"},
            },
        },
        tool_names=["tool-registry.readonly.inspection"],
        requires_approval=False,
    ),
    "change_assessment": AgentTemplate(
        template_id="change_assessment",
        agent_name="Change Assessment Agent",
        version="v1",
        status="published",
        description=(
            "Assess risk of cutover / parameter changes by cross-checking "
            "CMDB, historical tickets, and knowledge base SOPs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "change_id": {"type": "string"},
                "change_type": {"type": "string"},
                "affected_ne_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["change_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "risk_level": {"type": "string"},
                "risk_factors": {"type": "array"},
            },
        },
        tool_names=["cmdb.lookup", "ticket.history.search", "knowledge-api.chat.query"],
        requires_approval=True,
    ),
    "ticket_summary": AgentTemplate(
        template_id="ticket_summary",
        agent_name="Ticket Summary Agent",
        version="v1",
        status="published",
        description=(
            "Summarize and review a closed ticket: condense timeline, root "
            "cause, and remediation into a structured postmortem."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
            },
            "required": ["ticket_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "root_cause": {"type": "string"},
                "remediation": {"type": "string"},
            },
        },
        tool_names=["ticket.fetch", "knowledge-api.chat.query"],
        requires_approval=False,
    ),
}


@dataclass
class AgentPlatformStore:
    run_count: int = 0
    approval_task_count: int = 0
    runs: dict[str, AgentRunResponse] = field(default_factory=dict)
    approval_tasks: dict[str, ApprovalTask] = field(default_factory=dict)
    tools: dict[str, ToolResponse] = field(default_factory=dict)


def list_templates() -> list[AgentTemplate]:
    return list(TEMPLATES.values())


def create_run(store: AgentPlatformStore, payload: AgentRunCreate) -> AgentRunResponse:
    template = TEMPLATES[payload.template_id]
    store.run_count += 1
    run_id = f"agent_run_{store.run_count:03d}"
    requires_approval = template.requires_approval
    status = "waiting_approval" if requires_approval else "completed"
    approval_status = "pending" if requires_approval else "not_required"
    final_node = "ApprovalRequired" if requires_approval else "Completed"
    run = AgentRunResponse(
        run_id=run_id,
        template_id=template.template_id,
        agent_name=template.agent_name,
        status=status,
        trace_id=f"trace_{run_id}",
        requested_by=payload.requested_by,
        input=payload.input,
        output=_output_for_template(template.template_id, payload.input),
        node_trace=[
            NodeTrace(
                node_name="TemplateLoaded",
                status="completed",
                detail=f"Loaded {template.template_id}@{template.version}.",
            ),
            NodeTrace(
                node_name="RunStarted",
                status="completed",
                detail=f"Run requested by {payload.requested_by}.",
            ),
            NodeTrace(
                node_name="ToolPlan",
                status="completed",
                detail=f"Planned {len(template.tool_names)} tool calls.",
            ),
            NodeTrace(
                node_name=final_node,
                status="pending" if requires_approval else "completed",
                detail=(
                    "Human approval required before final write-back."
                    if requires_approval
                    else "Run completed with read-only tools."
                ),
            ),
        ],
        tool_calls=[
            ToolCallSummary(
                tool_name=tool_name,
                risk_level="approval_required" if requires_approval else "read_only",
                status="planned" if requires_approval else "completed",
            )
            for tool_name in template.tool_names
        ],
        approval_status=approval_status,
    )
    store.runs[run_id] = run
    record_event(
        action="run.created",
        actor=payload.requested_by,
        target_type="agent_run",
        target_id=run_id,
        payload={"template_id": template.template_id, "status": status},
    )
    if requires_approval:
        store.approval_task_count += 1
        task_id = f"approval_task_{store.approval_task_count:03d}"
        now = datetime.now(timezone.utc).isoformat()
        store.approval_tasks[task_id] = ApprovalTask(
            task_id=task_id,
            run_id=run_id,
            template_id=template.template_id,
            requested_by=payload.requested_by,
            status="pending",
            risk_level="approval_required",
            reason="Human approval required before final write-back.",
            created_at=now,
            updated_at=now,
            current_approver=payload.requested_by,
        )
    return run


def decide_approval_task(
    store: AgentPlatformStore,
    *,
    task_id: str,
    decision: str,
    decided_by: str,
    comment: str | None,
) -> ApprovalTask:
    task = _get_task(store, task_id)
    if not is_decidable(task):
        # Terminal / supplement-pending tasks cannot receive a decision.
        raise ApprovalTaskNotModifiable(task.status)
    updated_task = task.model_copy(
        update={
            "status": decision,
            "decided_by": decided_by,
            "comment": comment,
        }
    )
    store.approval_tasks[task_id] = updated_task
    record_event(
        action="approval.decided",
        actor=decided_by,
        target_type="approval_task",
        target_id=task_id,
        payload={"decision": decision, "comment": comment},
    )
    run = store.runs[task.run_id]
    approved = decision == "approved"
    updated_run = run.model_copy(
        update={
            "status": "completed" if approved else "failed",
            "approval_status": decision,
            "node_trace": [
                *run.node_trace,
                NodeTrace(
                    node_name="ApprovalApproved" if approved else "ApprovalRejected",
                    status="completed" if approved else "failed",
                    detail=comment or f"Approval {decision} by {decided_by}.",
                ),
            ],
            "tool_calls": [
                tool.model_copy(update={"status": "completed" if approved else "skipped"})
                for tool in run.tool_calls
            ],
            "output": _approved_output(run.output, approved),
        }
    )
    store.runs[run.run_id] = updated_run
    return updated_task


def request_supplement(
    store: AgentPlatformStore,
    *,
    task_id: str,
    question: str,
    requested_by: str,
) -> ApprovalTask:
    """HITL supplement: reviewer asks for more info.  Moves the task to
    ``pending_supplement`` so the requester can respond."""
    task = _get_task(store, task_id)
    _require_open(task)
    updated = task.model_copy(
        update={
            "status": "pending_supplement",
            "supplement_request": question,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store.approval_tasks[task_id] = updated
    return updated


def answer_supplement(
    store: AgentPlatformStore,
    *,
    task_id: str,
    answer: str,
    answered_by: str,
) -> ApprovalTask:
    """Agent / requester responds to the supplement.  Task returns to
    ``pending`` so the reviewer can decide again."""
    task = _get_task(store, task_id)
    if task.status != "pending_supplement":
        raise ValueError(f"task {task_id} is not in pending_supplement (got {task.status!r})")
    updated = task.model_copy(
        update={
            "status": "pending",
            "supplement_response": answer,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store.approval_tasks[task_id] = updated
    return updated


def route_approval(
    store: AgentPlatformStore,
    *,
    task_id: str,
    routed_to: str,
    routed_by: str,
    reason: str | None,
) -> ApprovalTask:
    """HITL routing: re-assign a pending approval to another reviewer
    (e.g. when the original assignee is on leave)."""
    task = _get_task(store, task_id)
    _require_open(task)
    updated = task.model_copy(
        update={
            "routed_to": routed_to,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store.approval_tasks[task_id] = updated
    record_event(
        action="approval.routed",
        actor=routed_by,
        target_type="approval_task",
        target_id=task_id,
        payload={"routed_to": routed_to, "reason": reason},
    )
    return updated


def expire_approval(
    store: AgentPlatformStore,
    *,
    task_id: str,
    escalation_reviewer: str | None,
) -> ApprovalTask:
    """HITL timeout: mark the task expired.  If an escalation reviewer is
    provided, also route to them.  The associated run moves to ``failed``
    so the requester can re-issue.  Terminal-state tasks (already approved
    / rejected / expired) cannot be timed out."""
    task = _get_task(store, task_id)
    _require_open(task)
    updated = task.model_copy(
        update={
            "status": "expired",
            "routed_to": escalation_reviewer,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store.approval_tasks[task_id] = updated
    if task.run_id in store.runs:
        run = store.runs[task.run_id]
        store.runs[task.run_id] = run.model_copy(
            update={"status": "failed", "approval_status": "expired"}
        )
    return updated


def delegate_approval(
    store: AgentPlatformStore,
    *,
    task_id: str,
    delegate: str,
    delegated_by: str,
    reason: str | None,
) -> ApprovalTask:
    """HITL delegation: add delegate as a co-reviewer.

    Unlike :func:`route_approval` (which moves ownership), delegation
    keeps the original requester as a valid decider.  Any of
    [requested_by, routed_to, delegates...] may decide; the first
    decision wins and subsequent attempts return 409.
    """
    task = _get_task(store, task_id)
    _require_open(task)
    new_delegates = list(dict.fromkeys([*task.delegates, delegate]))
    updated = task.model_copy(
        update={
            "delegates": new_delegates,
            "delegated_by": delegated_by,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store.approval_tasks[task_id] = updated
    _ = reason
    return updated


# --------------------------------------------------------------------------- #
# R20 governance: supplement / transfer / escalation
# --------------------------------------------------------------------------- #


class ApprovalTaskNotFound(LookupError):
    """Raised when a governance action targets an unknown task id."""


class ApprovalTaskNotSupplementable(ValueError):
    """Raised when a supplement is requested on a non-supplementable task."""


class ApprovalSupplementStateConflict(ValueError):
    """Raised when a supplement resolve is attempted in the wrong state."""


class ApprovalTransferForbidden(PermissionError):
    """Raised when a non-authorised user attempts to transfer a task."""


class ApprovalTaskNotModifiable(ValueError):
    """Raised when a legacy HITL action targets a terminal-state task."""


# Terminal states: a task that has reached one of these cannot be revived
# or further mutated by the legacy HITL actions (supplement / route /
# timeout / delegate).  Governance flows raise ``ApprovalTaskNotSupplementable``
# for the same condition.
_TERMINAL_STATUSES = frozenset({"approved", "rejected", "expired"})


def _get_task(store: AgentPlatformStore, task_id: str) -> ApprovalTask:
    """Look up an approval task or raise :class:`ApprovalTaskNotFound`."""
    task = store.approval_tasks.get(task_id)
    if task is None:
        raise ApprovalTaskNotFound(task_id)
    return task


def _require_open(task: ApprovalTask) -> None:
    """Reject terminal-state tasks for legacy mutating HITL actions."""
    if task.status in _TERMINAL_STATUSES:
        raise ApprovalTaskNotModifiable(task.status)


def request_supplement_governance(
    store: AgentPlatformStore,
    *,
    task_id: str,
    note: str,
    attachments: list[dict],
    requested_by: str,
) -> ApprovalTask:
    """R20-1: reviewer requests supplementary material.

    Moves ``pending -> supplement_pending`` and records the note plus
    requested attachments.  The requester later resolves the supplement
    via :func:`resolve_supplement_governance`.
    """
    task = store.approval_tasks.get(task_id)
    if task is None:
        raise ApprovalTaskNotFound(task_id)
    if task.status not in ("pending",):
        # Already-decided / expired / escalated tasks cannot be supplemented.
        raise ApprovalTaskNotSupplementable(task.status)
    now = datetime.now(timezone.utc).isoformat()
    updated = task.model_copy(
        update={
            "status": "supplement_pending",
            "supplement_note": note,
            "supplement_attachments": list(attachments),
            "supplement_requested_by": requested_by,
            "updated_at": now,
        }
    )
    store.approval_tasks[task_id] = updated
    record_event(
        action="approval.supplement_requested",
        actor=requested_by,
        target_type="approval_task",
        target_id=task_id,
        payload={"note": note, "attachments": len(attachments)},
    )
    return updated


def resolve_supplement_governance(
    store: AgentPlatformStore,
    *,
    task_id: str,
    attachments: list[dict],
    note: str | None,
    resolved_by: str,
) -> ApprovalTask:
    """R20-1: requester supplies the requested material.

    Moves ``supplement_pending -> pending`` so the reviewer can decide
    again.  Merges the supplied attachments onto the task.
    """
    task = store.approval_tasks.get(task_id)
    if task is None:
        raise ApprovalTaskNotFound(task_id)
    if task.status != "supplement_pending":
        raise ApprovalSupplementStateConflict(task.status)
    now = datetime.now(timezone.utc).isoformat()
    merged = [*task.supplement_attachments, *attachments]
    updated = task.model_copy(
        update={
            "status": "pending",
            "supplement_attachments": merged,
            "supplement_response": note,
            "supplement_resolved_by": resolved_by,
            "updated_at": now,
        }
    )
    store.approval_tasks[task_id] = updated
    record_event(
        action="approval.supplement_resolved",
        actor=resolved_by,
        target_type="approval_task",
        target_id=task_id,
        payload={"attachments": len(attachments)},
    )
    return updated


def transfer_approval(
    store: AgentPlatformStore,
    *,
    task_id: str,
    new_approver: str,
    reason: str,
    transferred_by: str,
    is_admin: bool = False,
) -> ApprovalTask:
    """R20-2: reassign the approval to a new approver.

    Permission: the current approver (``current_approver`` /
    ``requested_by``) or an admin may transfer.  Records a chronological
    entry in ``transfers`` and updates ``current_approver``.  The task
    stays decidable (returns to ``pending`` if it was ``transferred``).
    """
    task = store.approval_tasks.get(task_id)
    if task is None:
        raise ApprovalTaskNotFound(task_id)
    authorised = (
        is_admin or transferred_by == task.current_approver or transferred_by == task.requested_by
    )
    if not authorised:
        raise ApprovalTransferForbidden(transferred_by)
    if task.status in ("approved", "rejected", "expired"):
        raise ApprovalTaskNotSupplementable(task.status)
    now = datetime.now(timezone.utc).isoformat()
    history = list(task.transfers)
    history.append(
        {
            "from": task.current_approver or task.requested_by,
            "to": new_approver,
            "reason": reason,
            "transferred_by": transferred_by,
            "is_admin": is_admin,
            "ts": now,
        }
    )
    updated = task.model_copy(
        update={
            "status": "transferred",
            "current_approver": new_approver,
            "transfers": history,
            "routed_to": new_approver,
            "updated_at": now,
        }
    )
    store.approval_tasks[task_id] = updated
    record_event(
        action="approval.transferred",
        actor=transferred_by,
        target_type="approval_task",
        target_id=task_id,
        payload={"new_approver": new_approver, "reason": reason, "is_admin": is_admin},
    )
    return updated


def escalate_approval(
    store: AgentPlatformStore,
    *,
    task_id: str,
    escalated_to: str | None,
    reason: str | None,
    escalated_by: str | None,
) -> ApprovalTask:
    """R20-3: escalate an overdue / stuck approval.

    Marks the task ``escalated``, records the escalation reviewer and
    timestamp, and (when an escalation reviewer is supplied) routes the
    task to them.  The associated run is *not* failed — escalation is a
    signal that a higher-tier reviewer must now act.
    """
    task = store.approval_tasks.get(task_id)
    if task is None:
        raise ApprovalTaskNotFound(task_id)
    if task.status in ("approved", "rejected", "expired"):
        raise ApprovalTaskNotSupplementable(task.status)
    now = datetime.now(timezone.utc).isoformat()
    target = escalated_to or task.current_approver or task.requested_by
    update = {
        "status": "escalated",
        "escalated_at": now,
        "escalated_to": target,
        "escalation_reason": reason,
        "current_approver": target,
        "routed_to": target,
        "updated_at": now,
    }
    updated = task.model_copy(update=update)
    store.approval_tasks[task_id] = updated
    record_event(
        action="approval.escalated",
        actor=escalated_by or "system",
        target_type="approval_task",
        target_id=task_id,
        payload={"escalated_to": target, "reason": reason},
    )
    return updated


# Statuses from which a reviewer may still issue a final decision.  The
# governance sub-states (transferred / escalated / supplement_pending
# after resolve) all flow back to ``pending`` before deciding, but a
# ``transferred`` or ``escalated`` task that is still open is also
# decidable by the (new / escalation) approver.
_DECIDABLE_STATUSES = {"pending", "transferred", "escalated"}


def is_decidable(task: ApprovalTask) -> bool:
    """Return True when ``task`` may still receive a final decision."""
    return task.status in _DECIDABLE_STATUSES


def register_tool(store: AgentPlatformStore, payload: ToolRegistration) -> ToolResponse:
    tool = ToolResponse(**payload.model_dump(), health_status="unknown")
    store.tools[tool.tool_name] = tool
    record_event(
        action="tool.registered",
        actor=payload.tool_name,
        target_type="tool",
        target_id=tool.tool_name,
        payload={"service_name": tool.service_name, "risk_level": tool.risk_level},
    )
    return tool


def _output_for_template(template_id: str, payload: dict) -> dict:
    if template_id == "knowledge_qa":
        question = payload.get("question", "")
        return {
            "summary": f"Knowledge QA run prepared an answer for: {question}",
            "citations": [],
        }
    if template_id == "rca":
        return {
            "summary": "RCA run created hypotheses and is waiting for expert approval.",
            "hypotheses": [],
        }
    if template_id == "inspection":
        return {
            "summary": "Inspection run completed read-only checks.",
            "findings": [],
        }
    if template_id == "change_assessment":
        return {
            "summary": (
                f"Change assessment for {payload.get('change_id', 'unknown')} "
                "completed; awaiting expert review."
            ),
            "risk_level": "unknown",
            "risk_factors": [],
        }
    if template_id == "ticket_summary":
        return {
            "summary": (f"Ticket {payload.get('ticket_id', 'unknown')} summary prepared."),
            "root_cause": "",
            "remediation": "",
        }
    return {
        "summary": f"{template_id} run completed.",
        "findings": [],
    }


def _approved_output(output: dict, approved: bool) -> dict:
    if approved:
        return {
            **output,
            "approval_result": "approved",
            "summary": output.get("summary", "") + " Approval completed.",
        }
    return {
        **output,
        "approval_result": "rejected",
        "summary": output.get("summary", "") + " Approval rejected.",
    }


def run_to_persist_dict(run: AgentRunResponse) -> dict:
    """Serialise an AgentRunResponse to the AgentRunStore payload shape."""
    return {
        "run_id": run.run_id,
        "template_id": run.template_id,
        "agent_name": run.agent_name,
        "status": run.status,
        "trace_id": run.trace_id,
        "requested_by": run.requested_by,
        "input": run.input,
        "output": run.output,
        "node_trace": [n.model_dump() for n in run.node_trace],
        "tool_calls": [t.model_dump() for t in run.tool_calls],
        "approval_status": run.approval_status,
    }


def resume_run_from_node(
    store: AgentPlatformStore,
    run_id: str,
) -> AgentRunResponse:
    """Continue a paused run from its last completed node.

    Walks ``node_trace`` to find the first non-completed node and re-runs
    the rest of the pipeline. For MVP this advances the run to either
    ``Completed`` (no approval required) or keeps it ``waiting_approval``
    if the template requires human approval.  Persists the updated node
    trace via the runtime caller.
    """
    run = store.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    template = TEMPLATES[run.template_id]
    next_node = "ResumeNode"
    detail = f"Resumed {run_id} from checkpoint."
    new_trace = [
        *run.node_trace,
        NodeTrace(node_name=next_node, status="completed", detail=detail),
    ]
    if template.requires_approval:
        new_status = "waiting_approval"
        final_node = "ApprovalRequired"
        new_trace.append(
            NodeTrace(
                node_name=final_node,
                status="pending",
                detail="Resumed run still requires human approval.",
            )
        )
    else:
        new_status = "completed"
        final_node = "Completed"
        new_trace.append(
            NodeTrace(
                node_name=final_node,
                status="completed",
                detail="Resumed run completed read-only tools.",
            )
        )
    new_tool_calls = [
        tool.model_copy(update={"status": "planned" if template.requires_approval else "completed"})
        for tool in run.tool_calls
    ]
    updated = run.model_copy(
        update={
            "status": new_status,
            "node_trace": new_trace,
            "tool_calls": new_tool_calls,
        }
    )
    store.runs[run_id] = updated
    return updated


def select_runtime():
    """Pick the agent runtime backend from env (spec P3 §4 LangGraph v1).

    RUNTIME_BACKEND=dag (default) → self-built DAG (this module).
    RUNTIME_BACKEND=langgraph      → LangGraph v1 StateGraph runtime.

    Returns a lightweight handle exposing run(payload) so the HTTP
    layer can stay backend-agnostic.
    """
    import os

    backend = os.environ.get("RUNTIME_BACKEND", "dag").lower()
    if backend == "langgraph":
        from ai_employee.agent_platform_api.langgraph_runtime import build_langgraph_runtime

        return build_langgraph_runtime()
    return None  # sentinel: caller uses the default self-built DAG
