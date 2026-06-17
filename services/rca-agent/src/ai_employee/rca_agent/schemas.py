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


class RcaReportResponse(BaseModel):
    report_id: str
    run_id: str
    incident_id: str
    report_markdown: str
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    review_status: str
    final_root_cause: str | None = None


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
