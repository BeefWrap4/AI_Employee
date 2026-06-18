from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["running", "completed", "waiting_approval", "failed"]
ApprovalStatus = Literal["not_required", "pending", "approved", "rejected"]
TemplateStatus = Literal["published", "disabled"]
ApprovalTaskStatus = Literal["pending", "approved", "rejected"]
ApprovalDecision = Literal["approved", "rejected"]
ToolRiskLevel = Literal[
    "readonly", "suggest", "approval_required", "forbidden",
    "read_only", "high_risk",
]
ToolStatus = Literal["active", "disabled"]
ToolHealthStatus = Literal["unknown", "healthy", "unhealthy"]
EvalType = Literal["rag", "rca"]
EvalRunStatus = Literal["pending", "running", "completed", "failed"]


class AgentTemplate(BaseModel):
    template_id: str
    agent_name: str
    version: str
    status: TemplateStatus
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tool_names: list[str]
    requires_approval: bool


class AgentTemplateListResponse(BaseModel):
    items: list[AgentTemplate]
    total: int


class AgentRunCreate(BaseModel):
    template_id: str
    requested_by: str
    input: dict[str, Any] = Field(default_factory=dict)


class NodeTrace(BaseModel):
    node_name: str
    status: str
    detail: str


class ToolCallSummary(BaseModel):
    tool_name: str
    risk_level: Literal["read_only", "approval_required"]
    status: str


class AgentRunResponse(BaseModel):
    run_id: str
    template_id: str
    agent_name: str
    status: RunStatus
    trace_id: str
    requested_by: str
    input: dict[str, Any]
    output: dict[str, Any]
    node_trace: list[NodeTrace]
    tool_calls: list[ToolCallSummary]
    approval_status: ApprovalStatus


class AgentRunSummary(BaseModel):
    run_id: str
    template_id: str
    agent_name: str
    status: RunStatus
    trace_id: str
    requested_by: str
    approval_status: ApprovalStatus


class AgentRunListResponse(BaseModel):
    items: list[AgentRunSummary]
    total: int
    page: int
    page_size: int


class AgentRunResumeResponse(BaseModel):
    run: AgentRunResponse
    resumed_from_node: str


class ApprovalTask(BaseModel):
    task_id: str
    run_id: str
    template_id: str
    requested_by: str
    status: ApprovalTaskStatus
    risk_level: Literal["approval_required"]
    reason: str
    decided_by: str | None = None
    comment: str | None = None
    # HITL extensions (spec §5.4): supplement, routing, deadline, audit.
    supplement_request: str | None = None
    supplement_response: str | None = None
    assignee: str | None = None
    routed_to: str | None = None
    deadline_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Delegation (spec §5.4): list of co-reviewers.  Original requester
    # remains a valid decider; the first decision among them wins.
    delegates: list[str] = Field(default_factory=list)
    delegated_by: str | None = None


class ApprovalTaskListResponse(BaseModel):
    items: list[ApprovalTask]
    total: int
    page: int
    page_size: int


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    decided_by: str
    comment: str | None = None


class ApprovalSupplementRequest(BaseModel):
    question: str
    requested_by: str


class ApprovalSupplementAnswer(BaseModel):
    answer: str
    answered_by: str


class ApprovalRouteRequest(BaseModel):
    routed_to: str
    routed_by: str
    reason: str | None = None


class ApprovalTimeoutRequest(BaseModel):
    escalation_reviewer: str | None = None


class ApprovalDelegateRequest(BaseModel):
    delegate: str
    delegated_by: str
    reason: str | None = None


class ToolRegistration(BaseModel):
    tool_name: str
    service_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: ToolRiskLevel
    status: ToolStatus = "active"
    health_check_url: str | None = None


class ToolResponse(ToolRegistration):
    health_status: ToolHealthStatus = "unknown"


class ToolListResponse(BaseModel):
    items: list[ToolResponse]
    total: int
    page: int
    page_size: int


class AgentRunTraceResponse(BaseModel):
    run: AgentRunResponse
    template: AgentTemplate
    node_trace: list[NodeTrace]
    tool_calls: list[ToolCallSummary]
    approval_tasks: list[ApprovalTask]
    registered_tools: list[ToolResponse]


# --------------------------------------------------------------------------- #
# Eval center (spec §7)
# --------------------------------------------------------------------------- #


class EvalRunRequest(BaseModel):
    eval_type: EvalType
    template_id: str
    golden_path: str
    api_base: str | None = None


class EvalRunResponse(BaseModel):
    eval_run_id: str
    eval_type: EvalType
    template_id: str
    golden_path: str
    status: EvalRunStatus
    trace_id: str
    created_at: str
    completed_at: str | None = None
    report: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] | None = None
    error: str | None = None


class EvalRunListItem(BaseModel):
    eval_run_id: str
    eval_type: EvalType
    template_id: str
    golden_path: str
    status: EvalRunStatus
    trace_id: str
    created_at: str
    completed_at: str | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None


class EvalRunListResponse(BaseModel):
    items: list[EvalRunListItem]
    total: int
    page: int
    page_size: int
