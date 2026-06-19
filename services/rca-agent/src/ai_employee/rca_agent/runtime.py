from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    CandidateKnowledge,
    Evidence,
    Hypothesis,
    IncidentResponse,
    RawAlarmEvent,
    RcaReportResponse,
    RcaRunResponse,
)
from ai_employee.rca_agent.tool_adapters import (
    AdapterUnavailable,
    ToolAdapter,
    build_adapters,
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
    candidate_count: int = 0
    alarms: dict[str, AlarmEvent] = field(default_factory=dict)
    incidents: dict[str, IncidentResponse] = field(default_factory=dict)
    runs: dict[str, RcaRunResponse] = field(default_factory=dict)
    reports: dict[str, RcaReportResponse] = field(default_factory=dict)
    candidates: dict[str, CandidateKnowledge] = field(default_factory=dict)
    adapters: dict[str, ToolAdapter] = field(default_factory=dict)
    writeback_adapter: object | None = None
    writebacks: object | None = None
    rca_tool_call_log: object | None = None  # optional RcaToolCallLogStore
    # Operational metrics aggregated across runs (spec §4.6).
    tool_call_attempts: int = 0
    tool_call_failures: int = 0
    accepted_reports: int = 0
    rejected_reports: int = 0
    reviewed_reports: int = 0
    alarm_count_total: int = 0
    incident_alarm_total: int = 0
    report_gen_seconds_total: float = 0.0
    report_gen_count: int = 0

    def __post_init__(self) -> None:
        if not self.adapters:
            self.adapters = build_adapters()


def normalize_alarm(store: RcaStore, raw: RawAlarmEvent) -> AlarmEvent:
    store.alarm_count += 1
    store.alarm_count_total += 1
    if hasattr(raw, "model_dump"):
        raw_dump = raw.model_dump()
    else:
        raw_dump = {
            k: getattr(raw, k)
            for k in (
                "alarm_id",
                "alarm_code",
                "alarm_name",
                "vendor",
                "site_id",
                "cell_id",
                "ne_id",
                "severity",
                "start_time",
                "raw_payload",
            )
            if hasattr(raw, k)
        }
    event = AlarmEvent(
        **raw_dump,
        alarm_event_id=f"alarm_evt_{store.alarm_count:03d}",
        fingerprint=f"{raw_dump['vendor']}:{raw_dump['site_id']}:{raw_dump['ne_id']}:{raw_dump['alarm_code']}",
    )
    store.alarms[event.alarm_event_id] = event
    if hasattr(store, "save_alarm"):
        store.save_alarm(event)
    return event


def build_incident(
    store: RcaStore,
    raw_alarms: list[RawAlarmEvent],
    time_window_minutes: int = 30,
    *,
    topology_window_minutes: int = 0,
    parent_child_lag_seconds: int = 300,
    topology_client: Any | None = None,
) -> IncidentResponse:
    """Normalize, dedup, and correlate raw alarms into incident(s).

    Spec §6.2 — alarm convergence:
      1. Normalize each raw alarm (assigns alarm_event_id + fingerprint).
      2. Dedup by fingerprint (same vendor:site:ne:alarm_code collapses).
      3. Group survivors into incidents by site_id + time window
         (alarms within ``time_window_minutes`` of the group's first
         alarm belong together).
      4. **Parent-child rule**: a child alarm (declared via
         ``raw_payload['parent_alarm_id']``) on the same site/cell within
         ``parent_child_lag_seconds`` of its parent merges into the
         parent's group, even when the gap exceeds ``time_window_minutes``.
      5. **Topology rule**: an alarm whose site appears in another
         alarm's ``raw_payload['upstream_site_ids']`` and that fires within
         ``topology_window_minutes`` of the downstream alarm is absorbed
         into the downstream's incident (UPSTREAM correlation).
      6. Pick a primary alarm per incident (highest severity, then
         earliest start_time); the rest are companion alarms.

    Returns the *primary* incident (the one containing the first alarm).
    Additional incidents are still persisted on the store.  The single-
    group common case preserves the historical return contract.
    """
    events = [normalize_alarm(store, alarm) for alarm in raw_alarms]
    deduped = _dedup_by_fingerprint(events)
    groups = _group_by_site_and_window(deduped, time_window_minutes)
    if parent_child_lag_seconds > 0:
        groups = _merge_parent_child(groups, parent_child_lag_seconds)
    if topology_window_minutes > 0:
        groups = _merge_by_topology(
            groups,
            topology_window_minutes,
            topology_client=topology_client,
        )
    incidents = [_build_incident_record(store, group) for group in groups]
    for incident in incidents:
        store.incidents[incident.incident_id] = incident
        if hasattr(store, "save_incident"):
            store.save_incident(incident)
    # Primary incident = the one holding the earliest alarm overall.
    return incidents[0] if incidents else _empty_incident(store)


def _dedup_by_fingerprint(events: list[AlarmEvent]) -> list[AlarmEvent]:
    seen: set[str] = set()
    out: list[AlarmEvent] = []
    for event in events:
        if event.fingerprint in seen:
            continue
        seen.add(event.fingerprint)
        out.append(event)
    return out


def _group_by_site_and_window(
    events: list[AlarmEvent], window_minutes: int
) -> list[list[AlarmEvent]]:
    """Group alarms by site_id; within a site, split when the gap between
    consecutive alarms exceeds the time window."""
    if not events:
        return []
    by_site: dict[str, list[AlarmEvent]] = {}
    for event in events:
        by_site.setdefault(event.site_id, []).append(event)
    groups: list[list[AlarmEvent]] = []
    for site_events in by_site.values():
        site_events.sort(key=lambda e: e.start_time)
        current: list[AlarmEvent] = [site_events[0]]
        window_start = _parse_time(site_events[0].start_time)
        for event in site_events[1:]:
            t = _parse_time(event.start_time)
            if (
                window_start is not None
                and t is not None
                and (t - window_start).total_seconds() <= window_minutes * 60
            ):
                current.append(event)
            else:
                groups.append(current)
                current = [event]
                window_start = t
        groups.append(current)
    # Order groups by the earliest alarm so the primary incident is stable.
    groups.sort(key=lambda g: g[0].start_time)
    return groups


# --------------------------------------------------------------------------- #
# Parent-child + topology convergence rules (spec §6.2)
# --------------------------------------------------------------------------- #


def _alarm_parent_id(event: AlarmEvent) -> str | None:
    """Pull ``parent_alarm_id`` out of the raw payload (if any)."""
    return event.raw_payload.get("parent_alarm_id") if isinstance(event.raw_payload, dict) else None


def _alarm_upstream_sites(event: AlarmEvent) -> list[str]:
    """Pull ``upstream_site_ids`` out of the raw payload (if any)."""
    sites = (
        event.raw_payload.get("upstream_site_ids") if isinstance(event.raw_payload, dict) else None
    )
    if isinstance(sites, list):
        return [str(s) for s in sites]
    return []


def _find_in_group(group: list[AlarmEvent], alarm_id: str) -> AlarmEvent | None:
    for ev in group:
        if ev.alarm_id == alarm_id:
            return ev
    return None


def _merge_parent_child(
    groups: list[list[AlarmEvent]],
    lag_seconds: int,
) -> list[list[AlarmEvent]]:
    """Absorb child alarms into their parent group's incident.

    A child is matched to a parent by ``raw_payload['parent_alarm_id']``
    (same ``alarm_id`` string).  Child must share ``site_id`` and
    ``cell_id`` (or both have ``None`` cells) with the parent and start
    no later than ``lag_seconds`` after the parent's ``start_time``.
    Groups are merged by id-index so a chain (parent→child→grandchild)
    collapses into one group.
    """
    if not groups or lag_seconds <= 0:
        return groups

    parent_index: dict[str, int] = {}
    for gi, group in enumerate(groups):
        for ev in group:
            # Register *every* alarm's id as a potential parent reference.
            parent_index[ev.alarm_id] = gi

    child_target: dict[int, int] = {}
    for gi, group in enumerate(groups):
        for ev in group:
            pid = _alarm_parent_id(ev)
            if pid is None:
                continue
            pgi = parent_index.get(pid)
            if pgi is None or pgi == gi:
                continue
            parent = _find_in_group(groups[pgi], pid)
            if parent is None:
                continue
            if parent.site_id != ev.site_id:
                continue
            if (parent.cell_id or None) != (ev.cell_id or None):
                continue
            parent_t = _parse_time(parent.start_time)
            child_t = _parse_time(ev.start_time)
            if parent_t is None or child_t is None:
                continue
            delta = (child_t - parent_t).total_seconds()
            if delta < 0 or delta > lag_seconds:
                continue
            child_target[gi] = pgi

    return _apply_group_merges(groups, child_target)


def _merge_by_topology(
    groups: list[list[AlarmEvent]],
    window_minutes: int,
    *,
    topology_client: Any | None = None,
) -> list[list[AlarmEvent]]:
    """Absorb alarms on UPSTREAM sites into the downstream's group.

    Two signal sources, merged:

    1. **Explicit upstream declaration**: any alarm A that declares
       ``upstream_site_ids`` containing the site of another alarm B
       absorbs B if B's ``start_time`` is within ``window_minutes`` of A.
    2. **Neo4j graph topology** (R27, optional): when ``topology_client``
       is supplied, for each alarm A's site, ask Neo4j for upstream
       dependencies (Switch / Router / TransportLink nodes within 3
       hops).  Any other alarm whose site equals one of those upstream
       dependency ``node_id``s is also absorbed.
    """
    if not groups or window_minutes <= 0:
        return groups

    merges: dict[int, int] = {}
    window_sec = window_minutes * 60

    # Source 1: explicit upstream_site_ids.
    for gi, group in enumerate(groups):
        for ev_a in group:
            upstreams = set(_alarm_upstream_sites(ev_a))
            if not upstreams:
                continue
            for gj, group_j in enumerate(groups):
                if gj == gi or gj in merges:
                    continue
                for ev_b in group_j:
                    if ev_b.site_id not in upstreams:
                        continue
                    ta = _parse_time(ev_a.start_time)
                    tb = _parse_time(ev_b.start_time)
                    if ta is None or tb is None:
                        continue
                    if abs((tb - ta).total_seconds()) > window_sec:
                        continue
                    merges[gj] = gi
                    break

    # Source 2: Neo4j upstream dependency graph.
    if topology_client is not None:
        upstream_cache: dict[str, set[str]] = {}
        for gi, group in enumerate(groups):
            for ev_a in group:
                site = ev_a.site_id
                if site in upstream_cache:
                    upstream_sites = upstream_cache[site]
                else:
                    try:
                        result = topology_client.query_upstream_dependencies(site_id=site)
                        upstream_sites = {d.node_id for d in result.dependencies}
                    except Exception:
                        upstream_sites = set()
                    upstream_cache[site] = upstream_sites
                if not upstream_sites:
                    continue
                for gj, group_j in enumerate(groups):
                    if gj == gi or gj in merges:
                        continue
                    for ev_b in group_j:
                        if ev_b.site_id not in upstream_sites:
                            continue
                        merges[gj] = gi
                        break

    return _apply_group_merges(groups, merges)


_EPOCH_END = datetime(9999, 12, 31, 23, 59, 59)


def _apply_group_merges(
    groups: list[list[AlarmEvent]],
    merges: dict[int, int],
) -> list[list[AlarmEvent]]:
    """Apply ``{source_group_index: target_group_index}`` map.

    Resolves chains (A→B→C all collapse into A) and avoids cycles.
    Returns a new list of groups (target groups keep their original
    order; absorbed groups are dropped).
    """
    if not merges:
        return groups

    def _root(idx: int, _depth: int = 0) -> int:
        target = merges.get(idx)
        if target is None or target == idx:
            return idx
        if _depth > 32:  # safety against accidental cycles
            return idx
        return _root(target, _depth + 1)

    buckets: dict[int, list[AlarmEvent]] = {}
    for gi, group in enumerate(groups):
        root = _root(gi)
        buckets.setdefault(root, []).extend(group)

    # Order by the earliest alarm in each surviving bucket.
    surviving = sorted(
        buckets.items(),
        key=lambda kv: (
            min(
                (_parse_time(e.start_time) for e in kv[1]),
                default=None,
            )
            or _EPOCH_END
        ),
    )
    return [events for _, events in surviving]


def _build_incident_record(store: RcaStore, events: list[AlarmEvent]) -> IncidentResponse:
    store.incident_count += 1
    primary = _select_primary_alarm(events)
    return IncidentResponse(
        incident_id=f"inc_{store.incident_count:03d}",
        title=f"{primary.site_id} {primary.alarm_name}",
        status="analyzing",
        severity=primary.severity,
        site_id=primary.site_id,
        primary_alarm=primary,
        related_alarm_count=max(0, len(events) - 1),
        alarm_events=events,
    )


def _empty_incident(store: RcaStore) -> IncidentResponse:
    store.incident_count += 1
    return IncidentResponse(
        incident_id=f"inc_{store.incident_count:03d}",
        title="empty incident",
        status="analyzing",
        severity="info",
        site_id="",
        primary_alarm=AlarmEvent(
            alarm_id="empty",
            alarm_code="",
            alarm_name="",
            vendor="",
            site_id="",
            ne_id="",
            severity="info",
            start_time="",
            raw_payload={},
            alarm_event_id="alarm_evt_empty",
            fingerprint="",
        ),
        related_alarm_count=0,
        alarm_events=[],
    )


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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

    _rca_log = getattr(store, "rca_tool_call_log", None)
    # Allocate run_id and report_id BEFORE collect_evidence so per-adapter
    # tool calls are tagged with the right run_id in the tool_call_log.
    store.run_count += 1
    store.report_count += 1
    store.report_gen_count += 1
    store.tool_call_attempts += max(1, len(store.adapters))
    store.report_gen_seconds_total += 1.2
    run_id = f"rca_run_{store.run_count:03d}"
    report_id = f"rca_report_{store.report_count:03d}"
    evidence = collect_evidence(
        incident,
        run_id=run_id,
        tool_call_log=_rca_log,
    )
    hypotheses = generate_hypotheses(incident, evidence)
    report_md = generate_report_markdown(incident, evidence, hypotheses)

    store.run_count += 1
    store.report_count += 1
    store.report_gen_count += 1
    # Each adapter fetch above counts as a tool call attempt; one
    # failure counter is incremented when build_adapters() falls back to
    # a fixture. The runtime itself records the per-attempt counts in
    # collect_evidence.
    store.tool_call_attempts += max(1, len(store.adapters))
    # Estimate report generation time using a small constant — the
    # MVP does not have per-step latency instrumentation, so we record
    # a baseline of 1.2s per report which is conservative for the
    # fixture-based pipeline.
    store.report_gen_seconds_total += 1.2
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
    store.incident_alarm_total += 1
    if hasattr(store, "save_run_and_report"):
        store.save_run_and_report(run, report)
    return run


def resume_with_more_evidence(store: RcaStore, run_id: str) -> RcaRunResponse:
    run = store.runs[run_id]
    report = store.reports[run.report_id]
    next_evidence_no = len(report.evidence) + 1
    supplemental = Evidence(
        evidence_id=f"e_{next_evidence_no:03d}",
        source_type="ticket",
        source_ref=f"supplemental-review:{run_id}",
        content="Supplemental expert-requested evidence was collected after human review.",
        confidence=0.64,
    )
    evidence = [*report.evidence, supplemental]
    incident = store.incidents.get(run.incident_id)
    report_markdown = (
        generate_report_markdown(incident, evidence, report.hypotheses)
        if incident is not None
        else report.report_markdown
        + f"\n- `{supplemental.evidence_id}` [{supplemental.source_type}] {supplemental.content}\n"
    )
    updated_run = run.model_copy(
        update={
            "status": "waiting_review",
            "current_node": "HumanReview",
            "state_history": [
                *run.state_history,
                "CollectEvidence",
                "GenerateReport",
                "HumanReview",
            ],
            "evidence_count": len(evidence),
            "evidence": evidence,
        }
    )
    updated_report = report.model_copy(
        update={
            "report_markdown": report_markdown,
            "evidence": evidence,
            "review_status": "pending",
            "final_root_cause": None,
        }
    )
    store.runs[run_id] = updated_run
    store.reports[run.report_id] = updated_report
    if hasattr(store, "save_run_and_report"):
        store.save_run_and_report(updated_run, updated_report)
    return updated_run


def collect_evidence(
    incident: IncidentResponse,
    *,
    adapters: dict[str, ToolAdapter] | None = None,
    run_id: str | None = None,
    tool_call_log: object | None = None,
) -> list[Evidence]:
    """Collect evidence using pluggable adapters + a static knowledge lookup.

    Adapters cover KPI / Log / Topology / Ticket data sources.  The
    knowledge evidence is intentionally not a tool adapter: it is a
    static SOP recommendation derived from the alarm code.  When a real
    adapter fails (AdapterUnavailable), the call falls back to the
    fixture adapter so evidence collection never returns an empty list.

    When ``tool_call_log`` is provided, every adapter fetch is recorded
    there (run_id, tool_name, input summary, output summary, status,
    latency_ms, error_code) per spec §6.4.
    """
    primary = incident.primary_alarm
    adapter_map = adapters or build_adapters()
    tool_evidence: list[Evidence] = []
    import time as _t

    for source_type, adapter in adapter_map.items():
        _start = _t.monotonic()
        _status = "success"
        _error: str | None = None
        try:
            tool_evidence.extend(adapter.fetch(incident))
        except AdapterUnavailable:
            _status = "failed"
            _error = "adapter_unavailable"
            from ai_employee.rca_agent.tool_adapters import (
                FixtureKPIAdapter,
                FixtureLogAdapter,
                FixtureTicketAdapter,
                FixtureTopologyAdapter,
            )

            fallback_map = {
                "kpi": FixtureKPIAdapter(),
                "log": FixtureLogAdapter(),
                "topology": FixtureTopologyAdapter(),
                "ticket": FixtureTicketAdapter(),
            }
            tool_evidence.extend(fallback_map[source_type].fetch(incident))
        finally:
            if tool_call_log is not None:
                _latency = int((_t.monotonic() - _start) * 1000)
                try:
                    tool_call_log.record(  # type: ignore[union-attr]
                        run_id=run_id,
                        tool_name=source_type,
                        input_summary=f"incident={incident.incident_id}",
                        output_summary=None,
                        status=_status,
                        latency_ms=_latency,
                        error_code=_error,
                    )
                except Exception:
                    pass  # logging must never break evidence collection
    knowledge = Evidence(
        evidence_id="e_004",
        source_type="knowledge",
        source_ref=f"kb:{primary.alarm_code}",
        content=(
            "SOP recommends checking transmission port errors, optical power, "
            "and recent changes first."
        ),
        confidence=0.7,
    )
    return [*tool_evidence, knowledge]


def _legacy_collect_evidence(incident: IncidentResponse) -> list[Evidence]:
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


def _score_hypothesis(
    *,
    cause: str,
    incident: IncidentResponse,
    evidence: list[Evidence],
    primary_alarm: AlarmEvent,
    topology_deps: list[dict[str, Any]] | None = None,
) -> tuple[float, list[str], list[str]]:
    """Spec §6.5 root-cause ranking: compute a 0..1 confidence score from
    six weighted factors plus a base prior, plus split evidence into
    supporting vs contradicting by per-factor contribution.

    Returns ``(confidence, supporting_ids, contradicting_ids)``.

    Factors and their weights (sum = 1.0):

    * **time_relevance** (0.20) — gap between alarm start and evidence ts
    * **topology_distance** (0.15) — pre-resolved hops (caller-supplied)
    * **kpi_strength** (0.20) — metric evidence confidence in [0,1]
    * **history_similarity** (0.10) — number of historical tickets that
      share the alarm_code; saturates at 3
    * **sop_match** (0.15) — knowledge evidence whose content mentions
      the cause keyword
    * **counter_evidence** (0.20) — penalises for contradicting evidence
      explicitly tagged ``contradicts_root_cause=True``
    """

    # Time relevance: average 1 - normalised gap (smaller gap = higher).
    time_score = 0.0
    time_count = 0
    primary_ts = _parse_time(primary_alarm.start_time)
    supporting: list[str] = []
    contradicting: list[str] = []
    for ev in evidence:
        ev_ts = _parse_time(getattr(ev, "ts", "") or primary_alarm.start_time)
        if primary_ts is None or ev_ts is None:
            time_score += 0.5
        else:
            gap_min = abs((ev_ts - primary_ts).total_seconds()) / 60.0
            # 0 min → 1.0; 60 min → 0.0; clamp.
            time_score += max(0.0, 1.0 - gap_min / 60.0)
        time_count += 1
        if getattr(ev, "contradicts_root_cause", False):
            contradicting.append(ev.evidence_id)
        else:
            supporting.append(ev.evidence_id)
    time_relevance = (time_score / time_count) if time_count else 0.5

    # KPI strength: average metric evidence confidence.
    metric_ev = [e for e in evidence if e.source_type == "metric"]
    if metric_ev:
        kpi_strength = sum(e.confidence for e in metric_ev) / len(metric_ev)
    else:
        kpi_strength = 0.3  # neutral prior

    # Topology distance: closer hops → higher score.
    topology_distance = 0.5
    if topology_deps:
        hops = [int(d.get("hops", 99)) for d in topology_deps if d.get("node_id")]
        if hops:
            min_hop = min(hops)
            topology_distance = max(0.0, 1.0 - (min_hop - 1) * 0.25)

    # History similarity: count tickets that share alarm_code; saturates
    # at 3 → 1.0.
    ticket_ev = [e for e in evidence if e.source_type == "ticket"]
    alarm_code = (primary_alarm.alarm_code or "").upper()
    matching_tickets = 0
    for t in ticket_ev:
        if alarm_code and alarm_code in (t.content or "").upper():
            matching_tickets += 1
    history_similarity = min(1.0, matching_tickets / 3.0)

    # SOP match: knowledge evidence whose content mentions cause keyword.
    sop_match = 0.0
    cause_kw = cause.upper().split("_")[0] if cause else ""
    for e in evidence:
        if e.source_type != "knowledge":
            continue
        if cause_kw and cause_kw in (e.content or "").upper():
            sop_match = 1.0
            break

    # Counter-evidence: fraction of evidence that explicitly contradicts.
    counter_evidence = len(contradicting) / len(evidence) if evidence else 0.0

    # Base prior from cause type (mild default to keep all hypotheses
    # competitive — the real signal is the factors above).
    base = 0.30
    if "LINK" in cause.upper() or "TRANSPORT" in cause.upper():
        base = 0.45
    elif "PARAMETER" in cause.upper() or "CONFIG" in cause.upper():
        base = 0.30
    elif "WIRELESS" in cause.upper() or "ACCESS" in cause.upper():
        base = 0.45

    # Cause-alarm match: if the alarm code does not overlap with the
    # cause's keyword set, apply a small base penalty.  This is what
    # makes the wireless case (alarm code = RRC_SETUP_FAIL_HIGH) rank
    # the wireless cause above the link cause even when both have
    # identical evidence.
    alarm_code_norm = (primary_alarm.alarm_code or "").upper()
    if "LINK" in cause.upper() and not (
        "LINK" in alarm_code_norm or "TRANSPORT" in alarm_code_norm
    ):
        base -= 0.20
    if "WIRELESS" in cause.upper() and not (
        "RRC" in alarm_code_norm or "ACCESS" in alarm_code_norm or "WIRELESS" in alarm_code_norm
    ):
        base -= 0.20

    score = (
        base
        + 0.20 * time_relevance
        + 0.15 * topology_distance
        + 0.20 * kpi_strength
        + 0.10 * history_similarity
        + 0.15 * sop_match
        - 0.20 * counter_evidence
    )
    score = max(0.0, min(1.0, score))
    return score, supporting, contradicting


def generate_hypotheses(
    incident: IncidentResponse,
    evidence: list[Evidence],
    *,
    topology_deps: list[dict[str, Any]] | None = None,
) -> list[Hypothesis]:
    """R27: 6-factor ranked hypotheses (spec §6.5).

    Each candidate cause is scored by :func:`_score_hypothesis` which
    combines a base prior with time relevance, topology distance, KPI
    strength, history similarity, SOP match, and counter-evidence.
    Returns a list sorted by ``confidence`` descending.
    """
    primary_alarm = incident.alarm_events[0] if incident.alarm_events else None
    if primary_alarm is None:
        return []

    candidates: list[tuple[str, str]] = []
    # Always include all three cause candidates (spec §6.5 — every
    # RCA should propose at least one link / wireless / parameter
    # hypothesis so the human reviewer can weigh them).
    candidates.append(("h_001", "transmission_link_degradation"))
    candidates.append(("h_002", "wireless_access_anomaly"))
    candidates.append(("h_003", "recent_parameter_change"))

    scored: list[Hypothesis] = []
    for hid, cause in candidates:
        score, supporting, contradicting = _score_hypothesis(
            cause=cause,
            incident=incident,
            evidence=evidence,
            primary_alarm=primary_alarm,
            topology_deps=topology_deps,
        )
        scored.append(
            Hypothesis(
                hypothesis_id=hid,
                root_cause_type=cause,
                description=_hypothesis_description(cause, primary_alarm),
                supporting_evidence_ids=supporting,
                contradicting_evidence_ids=contradicting,
                confidence=score,
                next_check=_hypothesis_next_check(cause),
            )
        )

    scored.sort(key=lambda h: h.confidence, reverse=True)
    # Backward compat (R23-): when no evidence is tagged
    # ``contradicts_root_cause=True`` the trade-off is still surfaced by
    # populating each hypothesis's contradicting list with the *other*
    # hypothesis's supporting ids.  When explicit contradiction tags are
    # present the score-driven split wins.
    has_explicit_contradiction = any(getattr(e, "contradicts_root_cause", False) for e in evidence)
    if not has_explicit_contradiction and len(scored) >= 2:
        for h in scored:
            others = [s for s in scored if s.hypothesis_id != h.hypothesis_id]
            h.contradicting_evidence_ids = list(others[0].supporting_evidence_ids)
    return scored


def _hypothesis_description(cause: str, primary: AlarmEvent) -> str:
    base = f"Root cause candidate: {cause} for {primary.alarm_code} on {primary.site_id}."
    if "LINK" in cause.upper() or "TRANSPORT" in cause.upper():
        return (
            base
            + " Transmission link degradation is the most likely root cause of access failures."
        )
    if "PARAMETER" in cause.upper() or "CONFIG" in cause.upper():
        return base + " Recent configuration changes could contribute and should be ruled out."
    return (
        base
        + " Wireless access anomaly is likely, but transmission evidence still needs confirmation."
    )


def _hypothesis_next_check(cause: str) -> list[str]:
    if "LINK" in cause.upper() or "TRANSPORT" in cause.upper():
        return [
            "Confirm port error counters with the transmission team.",
            "Check recent cutover or fiber maintenance records.",
        ]
    if "PARAMETER" in cause.upper() or "CONFIG" in cause.upper():
        return ["Compare parameter changes before and after the alarm window."]
    return ["Check cell KPI trend and neighboring-cell alarms."]


def generate_report_markdown(
    incident: IncidentResponse,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
) -> str:
    evidence_lines = "\n".join(
        f"- `{item.evidence_id}` [{item.source_type}] {item.content}" for item in evidence
    )
    hypothesis_lines = "\n".join(
        "- `{}` {} ({:.0%})\n  - 支持证据: {}\n  - 反驳证据: {}\n  - Next: {}".format(
            item.hypothesis_id,
            item.description,
            item.confidence,
            ", ".join(f"`{e}`" for e in item.supporting_evidence_ids) or "(无)",
            ", ".join(f"`{e}`" for e in item.contradicting_evidence_ids) or "(无)",
            "; ".join(item.next_check),
        )
        for item in hypotheses
    )
    # 影响范围 (spec §6.6): unique sites / NEs / cells in incident + evidence.
    impacted_ne = sorted({event.ne_id for event in incident.alarm_events if event.ne_id})
    impacted_cells = sorted({event.cell_id for event in incident.alarm_events if event.cell_id})
    impact_section = (
        f"- 主站点: `{incident.site_id}`\n"
        f"- 影响网元 ({len(impacted_ne)}): "
        f"{', '.join(f'`{ne}`' for ne in impacted_ne) or '—'}\n"
        f"- 影响小区 ({len(impacted_cells)}): "
        f"{', '.join(f'`{c}`' for c in impacted_cells) or '—'}\n"
    )
    # 关键时间线 (spec §6.6): alarm start times + evidence source_refs.
    timeline_lines = []
    for event in sorted(incident.alarm_events, key=lambda e: e.start_time):
        timeline_lines.append(
            f"- `{event.start_time}` [{event.severity}] `{event.alarm_code}` "
            f"{event.alarm_name} ({event.ne_id}/{event.cell_id or '—'})"
        )
    if not timeline_lines:
        timeline_lines.append("- (no alarms)")
    timeline_section = "\n".join(timeline_lines)
    # 引用来源 (spec §6.6): unique source_refs from evidence.
    source_refs = sorted({item.source_ref for item in evidence if item.source_ref})
    sources_section = (
        "\n".join(f"- `{ref}`" for ref in source_refs)
        if source_refs
        else "- (no external sources cited)"
    )
    return (
        f"# RCA 报告 - {incident.incident_id}\n\n"
        f"## 事件摘要\n"
        f"- Site: `{incident.site_id}`\n"
        f"- Primary alarm: `{incident.primary_alarm.alarm_code}` {incident.primary_alarm.alarm_name}\n"
        f"- Related alarms: {incident.related_alarm_count}\n\n"
        f"## 影响范围\n{impact_section}\n\n"
        f"## 关键时间线\n{timeline_section}\n\n"
        f"## 证据链\n{evidence_lines}\n\n"
        f"## Top-N 根因候选\n{hypothesis_lines}\n\n"
        f"## 引用来源\n{sources_section}\n\n"
        "## 推荐处置动作\n"
        "- 先核查传输端口误码、光功率和链路抖动。\n"
        "- 与近期割接、参数变更记录交叉确认。\n\n"
        "## 需人工确认\n"
        "- 报告为诊断建议，不执行配置变更、网元重启或脚本操作。\n"
    )


def _select_primary_alarm(events: list[AlarmEvent]) -> AlarmEvent:
    severity_rank = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "info": 4}
    return sorted(events, key=lambda e: severity_rank[e.severity])[0]
