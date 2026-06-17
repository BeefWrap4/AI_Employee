from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["running", "completed", "waiting_approval", "failed"]
ApprovalStatus = Literal["not_required", "pending", "approved", "rejected"]
TemplateStatus = Literal["published", "disabled"]
ApprovalTaskStatus = Literal["pending", "approved", "rejected"]
ApprovalDecision = Literal["approved", "rejected"]
ToolRiskLevel = Literal["read_only", "approval_required", "high_risk"]
ToolStatus = Literal["active", "disabled"]
ToolHealthStatus = Literal["unknown", "healthy", "unhealthy"]


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


class ApprovalTaskListResponse(BaseModel):
    items: list[ApprovalTask]
    total: int
    page: int
    page_size: int


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    decided_by: str
    comment: str | None = None


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
