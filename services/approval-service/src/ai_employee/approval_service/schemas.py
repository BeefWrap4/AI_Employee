"""Pydantic schemas for the approval-service.

These mirror the agent-platform ``ApprovalTask`` contract (R20 unified
state machine) so consumers can switch to the service with no contract
drift.  Kept local to the service so it can be deployed independently
without importing the platform package.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ApprovalTaskStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "supplement_pending",
    "transferred",
    "escalated",
    "pending_supplement",
    "expired",
]
ApprovalDecision = Literal["approved", "rejected"]


class ApprovalTaskCreate(BaseModel):
    task_id: str
    run_id: str
    template_id: str
    requested_by: str
    risk_level: Literal["approval_required"]
    reason: str = ""
    current_approver: str | None = None
    created_at: str | None = None


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
    supplement_request: str | None = None
    supplement_response: str | None = None
    assignee: str | None = None
    routed_to: str | None = None
    deadline_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    delegates: list[str] = Field(default_factory=list)
    delegated_by: str | None = None
    supplement_note: str | None = None
    supplement_attachments: list[dict[str, Any]] = Field(default_factory=list)
    supplement_requested_by: str | None = None
    supplement_resolved_by: str | None = None
    transfers: list[dict[str, Any]] = Field(default_factory=list)
    current_approver: str | None = None
    escalated_at: str | None = None
    escalated_to: str | None = None
    escalation_reason: str | None = None


class ApprovalTaskListResponse(BaseModel):
    items: list[ApprovalTask]
    total: int
    page: int
    page_size: int


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    decided_by: str
    comment: str | None = None


class SupplementAttachment(BaseModel):
    name: str
    uri: str
    content_type: str | None = None


class ApprovalSupplementGovernanceRequest(BaseModel):
    note: str
    attachments: list[SupplementAttachment] = Field(default_factory=list)
    requested_by: str


class ApprovalSupplementResolveRequest(BaseModel):
    attachments: list[SupplementAttachment] = Field(default_factory=list)
    note: str | None = None
    resolved_by: str


class ApprovalTransferRequest(BaseModel):
    new_approver: str
    reason: str
    transferred_by: str
    is_admin: bool = False


class ApprovalEscalateRequest(BaseModel):
    escalated_to: str | None = None
    reason: str | None = None
    escalated_by: str | None = None


__all__ = [
    "ApprovalDecision",
    "ApprovalDecisionRequest",
    "ApprovalEscalateRequest",
    "ApprovalSupplementGovernanceRequest",
    "ApprovalSupplementResolveRequest",
    "ApprovalTask",
    "ApprovalTaskCreate",
    "ApprovalTaskListResponse",
    "ApprovalTaskStatus",
    "ApprovalTransferRequest",
    "SupplementAttachment",
]
