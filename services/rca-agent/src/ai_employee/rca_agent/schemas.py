from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "major", "minor", "warning", "info"]
RunStatus = Literal[
    "running",
    "waiting_review",
    "need_more_evidence",
    "failed",
    "accepted",
    "rejected",
]
ReviewDecision = Literal["accepted", "rejected", "need_more_evidence"]


class RawAlarmEvent(BaseModel):
    alarm_id: str
    alarm_code: str
    alarm_name: str
    vendor: str
    site_id: str
    cell_id: str | None = None
    ne_id: str
    severity: Severity = "major"
    start_time: str
    clear_time: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class AlarmEvent(RawAlarmEvent):
    alarm_event_id: str
    fingerprint: str


class IncidentBuildRequest(BaseModel):
    alarms: list[RawAlarmEvent] = Field(min_length=1)
    time_window_minutes: int = Field(default=30, ge=1, le=240)
    # R26: expose spec §6.2 convergence knobs (parent-child + topology)
    # so the API caller can drive the full convergence pipeline.  Both
    # default to 0 (off) to preserve backward compat with pre-R26 callers.
    topology_window_minutes: int = Field(default=0, ge=0, le=240)
    parent_child_lag_seconds: int = Field(default=300, ge=0, le=3600)


class IncidentResponse(BaseModel):
    incident_id: str
    title: str
    status: str
    severity: Severity
    site_id: str
    primary_alarm: AlarmEvent
    related_alarm_count: int
    alarm_events: list[AlarmEvent]


class Evidence(BaseModel):
    evidence_id: str
    source_type: Literal["metric", "log", "topology", "knowledge", "ticket"]
    source_ref: str
    content: str
    confidence: float = Field(ge=0, le=1)
    # R27: optional time + contradiction flag for the 6-factor scorer.
    ts: str | None = None
    contradicts_root_cause: bool = False


class Hypothesis(BaseModel):
    hypothesis_id: str
    root_cause_type: str
    description: str
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    next_check: list[str] = Field(default_factory=list)


class RcaRunCreate(BaseModel):
    incident_id: str | None = None
    mode: str = "auto_collect"
    max_tool_calls: int = Field(default=20, ge=1, le=50)
    require_human_review: bool = True
    alarms: list[RawAlarmEvent] = Field(default_factory=list)


class RcaRunResponse(BaseModel):
    run_id: str
    incident_id: str
    report_id: str
    status: RunStatus
    current_node: str
    trace_id: str
    state_history: list[str]
    evidence_count: int
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    error: str | None = None


class RcaRunSummary(BaseModel):
    run_id: str
    incident_id: str
    report_id: str
    status: RunStatus
    current_node: str
    trace_id: str
    evidence_count: int
    hypothesis_count: int


class RcaRunListResponse(BaseModel):
    items: list[RcaRunSummary]
    total: int
    page: int
    page_size: int


class RcaReportResponse(BaseModel):
    report_id: str
    run_id: str
    incident_id: str
    report_markdown: str
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    review_status: str
    final_root_cause: str | None = None


class RcaReportSummary(BaseModel):
    report_id: str
    run_id: str
    incident_id: str
    review_status: str
    final_root_cause: str | None = None
    evidence_count: int
    hypothesis_count: int


class RcaReportListResponse(BaseModel):
    items: list[RcaReportSummary]
    total: int
    page: int
    page_size: int


class ReportReviewRequest(BaseModel):
    decision: ReviewDecision
    final_root_cause: str | None = None
    reviewer: str | None = None
    comment: str | None = None


class ReportReviewResponse(BaseModel):
    report_id: str
    review_status: ReviewDecision
    final_root_cause: str | None = None
    reviewer: str | None = None
    comment: str | None = None


CandidateReviewStatus = Literal["pending", "approved", "rejected"]
CandidateReviewDecision = Literal["approved", "rejected"]


class CandidateKnowledge(BaseModel):
    candidate_id: str
    source_report_id: str
    source_incident_id: str
    hypothesis_id: str
    root_cause_type: str
    title: str
    content: str
    evidence_summary: str
    review_status: CandidateReviewStatus = "pending"
    reviewer: str | None = None
    review_comment: str | None = None
    imported_doc_id: str | None = None
    created_at: str
    reviewed_at: str | None = None


class CandidateListResponse(BaseModel):
    items: list[CandidateKnowledge]
    total: int
    page: int
    page_size: int


class CandidateReviewRequest(BaseModel):
    decision: CandidateReviewDecision
    reviewer: str | None = None
    comment: str | None = None


class CandidateReviewResponse(BaseModel):
    candidate_id: str
    review_status: CandidateReviewStatus
    reviewer: str | None = None
    review_comment: str | None = None
    reviewed_at: str | None = None


# --------------------------------------------------------------------------- #
# Ticket write-back (spec §6.4)
# --------------------------------------------------------------------------- #


class TicketWritebackRequest(BaseModel):
    rca_report_id: str


class TicketWritebackResponse(BaseModel):
    ticket_id: str
    rca_report_id: str
    incident_id: str
    adapter_name: str
    status: str
    response: dict[str, Any] = Field(default_factory=dict)
    attempt_id: str
