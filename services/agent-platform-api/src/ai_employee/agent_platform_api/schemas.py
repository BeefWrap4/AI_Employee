from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["running", "completed", "waiting_approval", "failed"]
ApprovalStatus = Literal["not_required", "pending", "approved", "rejected"]
TemplateStatus = Literal["published", "disabled"]


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
