from __future__ import annotations

from dataclasses import dataclass, field

from ai_employee.agent_platform_api.schemas import (
    AgentRunCreate,
    AgentRunResponse,
    AgentTemplate,
    ApprovalTask,
    NodeTrace,
    ToolCallSummary,
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
}


@dataclass
class AgentPlatformStore:
    run_count: int = 0
    approval_task_count: int = 0
    runs: dict[str, AgentRunResponse] = field(default_factory=dict)
    approval_tasks: dict[str, ApprovalTask] = field(default_factory=dict)


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
    if requires_approval:
        store.approval_task_count += 1
        task_id = f"approval_task_{store.approval_task_count:03d}"
        store.approval_tasks[task_id] = ApprovalTask(
            task_id=task_id,
            run_id=run_id,
            template_id=template.template_id,
            requested_by=payload.requested_by,
            status="pending",
            risk_level="approval_required",
            reason="Human approval required before final write-back.",
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
    task = store.approval_tasks[task_id]
    updated_task = task.model_copy(
        update={
            "status": decision,
            "decided_by": decided_by,
            "comment": comment,
        }
    )
    store.approval_tasks[task_id] = updated_task
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
    return {
        "summary": "Inspection run completed read-only checks.",
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
