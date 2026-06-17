from __future__ import annotations

from dataclasses import dataclass, field

from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    Evidence,
    Hypothesis,
    IncidentResponse,
    RawAlarmEvent,
    RcaReportResponse,
    RcaRunResponse,
)


STATE_HISTORY = [
    "AlarmReceived",
    "Normalized",
    "IncidentBuilt",
    "Triage",
    "BuildPlan",
    "CollectEvidence",
    "GenerateHypotheses",
    "VerifyHypotheses",
    "RankRootCause",
    "GenerateReport",
    "HumanReview",
]


@dataclass
class RcaStore:
    alarm_count: int = 0
    incident_count: int = 0
    run_count: int = 0
    report_count: int = 0
    alarms: dict[str, AlarmEvent] = field(default_factory=dict)
    incidents: dict[str, IncidentResponse] = field(default_factory=dict)
    runs: dict[str, RcaRunResponse] = field(default_factory=dict)
    reports: dict[str, RcaReportResponse] = field(default_factory=dict)


def normalize_alarm(store: RcaStore, raw: RawAlarmEvent) -> AlarmEvent:
    store.alarm_count += 1
    event = AlarmEvent(
        **raw.model_dump(),
        alarm_event_id=f"alarm_evt_{store.alarm_count:03d}",
        fingerprint=f"{raw.vendor}:{raw.site_id}:{raw.ne_id}:{raw.alarm_code}",
    )
    store.alarms[event.alarm_event_id] = event
    if hasattr(store, "save_alarm"):
        store.save_alarm(event)
    return event


def build_incident(
    store: RcaStore,
    raw_alarms: list[RawAlarmEvent],
    time_window_minutes: int = 30,
) -> IncidentResponse:
    del time_window_minutes
    events = [normalize_alarm(store, alarm) for alarm in raw_alarms]
    primary = _select_primary_alarm(events)
    store.incident_count += 1
    incident = IncidentResponse(
        incident_id=f"inc_{store.incident_count:03d}",
        title=f"{primary.site_id} {primary.alarm_name}",
        status="analyzing",
        severity=primary.severity,
        site_id=primary.site_id,
        primary_alarm=primary,
        related_alarm_count=max(0, len(events) - 1),
        alarm_events=events,
    )
    store.incidents[incident.incident_id] = incident
    if hasattr(store, "save_incident"):
        store.save_incident(incident)
    return incident


def run_rca(
    store: RcaStore,
    *,
    raw_alarms: list[RawAlarmEvent],
    incident_id: str | None,
    require_human_review: bool,
) -> RcaRunResponse:
    if incident_id and incident_id in store.incidents:
        incident = store.incidents[incident_id]
    else:
        incident = build_incident(store, raw_alarms)

    evidence = collect_evidence(incident)
    hypotheses = generate_hypotheses(incident, evidence)
    report_md = generate_report_markdown(incident, evidence, hypotheses)

    store.run_count += 1
    store.report_count += 1
    run_id = f"rca_run_{store.run_count:03d}"
    report_id = f"rca_report_{store.report_count:03d}"
    status = "waiting_review" if require_human_review else "accepted"
    run = RcaRunResponse(
        run_id=run_id,
        incident_id=incident.incident_id,
        report_id=report_id,
        status=status,
        current_node="HumanReview",
        trace_id=f"trace_{run_id}",
        state_history=list(STATE_HISTORY),
        evidence_count=len(evidence),
        evidence=evidence,
        hypotheses=hypotheses,
    )
    report = RcaReportResponse(
        report_id=report_id,
        run_id=run_id,
        incident_id=incident.incident_id,
        report_markdown=report_md,
        hypotheses=hypotheses,
        evidence=evidence,
        review_status="pending" if require_human_review else "accepted",
    )
    store.runs[run_id] = run
    store.reports[report_id] = report
    if hasattr(store, "save_run_and_report"):
        store.save_run_and_report(run, report)
    return run


def collect_evidence(incident: IncidentResponse) -> list[Evidence]:
    primary = incident.primary_alarm
    return [
        Evidence(
            evidence_id="e_001",
            source_type="metric",
            source_ref=f"kpi:{primary.site_id}:{primary.cell_id or 'site'}",
            content="RRC setup failure rate and transport error counters increased in the alarm window.",
            confidence=0.82,
        ),
        Evidence(
            evidence_id="e_002",
            source_type="log",
            source_ref=f"log:{primary.ne_id}",
            content=f"NE logs include {primary.alarm_code} near the incident start time.",
            confidence=0.76,
        ),
        Evidence(
            evidence_id="e_003",
            source_type="topology",
            source_ref=f"topology:{primary.site_id}",
            content="Affected cell depends on the same upstream transmission path as the primary alarm.",
            confidence=0.78,
        ),
        Evidence(
            evidence_id="e_004",
            source_type="knowledge",
            source_ref=f"kb:{primary.alarm_code}",
            content="SOP recommends checking transmission port errors, optical power, and recent changes first.",
            confidence=0.7,
        ),
        Evidence(
            evidence_id="e_005",
            source_type="ticket",
            source_ref=f"ticket-history:{primary.site_id}",
            content="Similar historical cases at the site were closed as transmission link degradation.",
            confidence=0.66,
        ),
    ]


def generate_hypotheses(
    incident: IncidentResponse,
    evidence: list[Evidence],
) -> list[Hypothesis]:
    alarm_codes = {event.alarm_code.upper() for event in incident.alarm_events}
    if any("LINK" in code or "TRANSPORT" in code for code in alarm_codes):
        primary = Hypothesis(
            hypothesis_id="h_001",
            root_cause_type="transmission_link_degradation",
            description="Transmission link degradation is the most likely root cause of access failures.",
            supporting_evidence_ids=["e_001", "e_003", "e_004", "e_005"],
            confidence=0.78,
            next_check=[
                "Confirm port error counters with the transmission team.",
                "Check recent cutover or fiber maintenance records.",
            ],
        )
    else:
        primary = Hypothesis(
            hypothesis_id="h_001",
            root_cause_type="wireless_access_anomaly",
            description="Wireless access anomaly is likely, but transmission evidence still needs confirmation.",
            supporting_evidence_ids=["e_001", "e_002", "e_004"],
            confidence=0.62,
            next_check=["Check cell KPI trend and neighboring-cell alarms."],
        )
    secondary = Hypothesis(
        hypothesis_id="h_002",
        root_cause_type="recent_parameter_change",
        description="Recent configuration changes could contribute and should be ruled out.",
        supporting_evidence_ids=[evidence[1].evidence_id, evidence[4].evidence_id],
        confidence=0.46,
        next_check=["Compare parameter changes before and after the alarm window."],
    )
    return [primary, secondary]


def generate_report_markdown(
    incident: IncidentResponse,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
) -> str:
    evidence_lines = "\n".join(
        f"- `{item.evidence_id}` [{item.source_type}] {item.content}" for item in evidence
    )
    hypothesis_lines = "\n".join(
        "- `{}` {} ({:.0%})\n  - Evidence: {}\n  - Next: {}".format(
            item.hypothesis_id,
            item.description,
            item.confidence,
            ", ".join(f"`{e}`" for e in item.supporting_evidence_ids),
            "; ".join(item.next_check),
        )
        for item in hypotheses
    )
    return (
        f"# RCA 报告 - {incident.incident_id}\n\n"
        f"## 事件摘要\n"
        f"- Site: `{incident.site_id}`\n"
        f"- Primary alarm: `{incident.primary_alarm.alarm_code}` {incident.primary_alarm.alarm_name}\n"
        f"- Related alarms: {incident.related_alarm_count}\n\n"
        f"## 证据链\n{evidence_lines}\n\n"
        f"## Top-N 根因候选\n{hypothesis_lines}\n\n"
        "## 推荐处置动作\n"
        "- 先核查传输端口误码、光功率和链路抖动。\n"
        "- 与近期割接、参数变更记录交叉确认。\n\n"
        "## 需人工确认\n"
        "- 报告为诊断建议，不执行配置变更、网元重启或脚本操作。\n"
    )


def _select_primary_alarm(events: list[AlarmEvent]) -> AlarmEvent:
    severity_rank = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "info": 4}
    return sorted(events, key=lambda e: severity_rank[e.severity])[0]
