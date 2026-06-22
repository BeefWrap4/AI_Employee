"""RCA alarm correlation / incident building tests (spec §6.2).

Covers dedup by fingerprint, time-window grouping, primary/companion
selection, and topology-aware aggregation — the gap flagged in the
gap analysis (`del time_window_minutes` was a stub).
"""

from __future__ import annotations

from ai_employee.rca_agent.runtime import RcaStore, build_incident
from ai_employee.rca_agent.schemas import RawAlarmEvent


def _alarm(
    alarm_id: str,
    code: str = "LINK_DEGRADE",
    site: str = "SITE-001",
    ne: str = "NE-001",
    severity: str = "major",
    start: str = "2026-06-17T10:00:00+08:00",
    cell: str | None = "CELL-001",
) -> RawAlarmEvent:
    return RawAlarmEvent(
        alarm_id=alarm_id,
        alarm_code=code,
        alarm_name=f"{code} alarm",
        vendor="huawei",
        site_id=site,
        cell_id=cell,
        ne_id=ne,
        severity=severity,
        start_time=start,
        raw_payload={},
    )


def test_dedup_drops_duplicate_fingerprints() -> None:
    """Two alarms with identical (vendor:site:ne:code) fingerprint collapse."""
    store = RcaStore()
    alarms = [
        _alarm("a_001", code="LINK_DEGRADE"),
        _alarm("a_002", code="LINK_DEGRADE"),  # same fingerprint → duplicate
    ]
    incident = build_incident(store, alarms, time_window_minutes=30)
    assert len(incident.alarm_events) == 1
    assert incident.related_alarm_count == 0


def test_distinct_alarms_within_window_group_into_one_incident() -> None:
    store = RcaStore()
    alarms = [
        _alarm(
            "a_001", code="LINK_DEGRADE", severity="critical", start="2026-06-17T10:00:00+08:00"
        ),
        _alarm("a_002", code="RRC_SETUP_FAIL", severity="major", start="2026-06-17T10:10:00+08:00"),
    ]
    incident = build_incident(store, alarms, time_window_minutes=30)
    assert len(incident.alarm_events) == 2
    assert incident.related_alarm_count == 1
    # Primary = highest severity (critical).
    assert incident.primary_alarm.alarm_code == "LINK_DEGRADE"


def test_alarms_outside_window_split_into_separate_incidents() -> None:
    store = RcaStore()
    alarms = [
        _alarm("a_001", code="LINK_DEGRADE", start="2026-06-17T10:00:00+08:00"),
        _alarm("a_002", code="RRC_SETUP_FAIL", start="2026-06-17T11:30:00+08:00"),
    ]
    incidents = build_incident(store, alarms, time_window_minutes=30)
    # Outside the 30-minute window → two incidents.  build_incident returns
    # the primary incident but the store now holds two.
    assert store.incident_count == 2


def test_primary_selected_by_severity_then_earliest_time() -> None:
    store = RcaStore()
    alarms = [
        _alarm("a_001", code="RRC_SETUP_FAIL", severity="minor", start="2026-06-17T10:00:00+08:00"),
        _alarm(
            "a_002", code="LINK_DEGRADE", severity="critical", start="2026-06-17T10:05:00+08:00"
        ),
        _alarm("a_003", code="POWER_ALARM", severity="critical", start="2026-06-17T10:02:00+08:00"),
    ]
    incident = build_incident(store, alarms, time_window_minutes=30)
    # Two criticals → earliest one wins.
    assert incident.primary_alarm.alarm_code == "POWER_ALARM"
    assert incident.primary_alarm.severity == "critical"


def test_different_sites_do_not_merge() -> None:
    store = RcaStore()
    alarms = [
        _alarm("a_001", code="LINK_DEGRADE", site="SITE-001"),
        _alarm("a_002", code="LINK_DEGRADE", site="SITE-002"),
    ]
    build_incident(store, alarms, time_window_minutes=30)
    assert store.incident_count == 2


def test_companion_alarms_marked_relative_to_primary() -> None:
    """Companion alarms are retained in alarm_events but only primary is
    surfaced as primary_alarm; related_alarm_count excludes deduped dupes."""
    store = RcaStore()
    alarms = [
        _alarm("a_001", code="LINK_DEGRADE", severity="critical"),
        _alarm("a_002", code="RRC_SETUP_FAIL", severity="major"),
        _alarm("a_003", code="LINK_DEGRADE", severity="critical"),  # dup of a_001
    ]
    incident = build_incident(store, alarms, time_window_minutes=30)
    assert len(incident.alarm_events) == 2  # a_003 deduped
    assert incident.related_alarm_count == 1
    assert incident.primary_alarm.alarm_id == "a_001"


def test_time_window_minutes_actually_used() -> None:
    """A 10-minute gap is within a 30-min window but outside a 5-min window."""
    store = RcaStore()
    alarms = [
        _alarm("a_001", code="LINK_DEGRADE", start="2026-06-17T10:00:00+08:00"),
        _alarm("a_002", code="RRC_SETUP_FAIL", start="2026-06-17T10:08:00+08:00"),
    ]
    # 8-minute gap: within 30 → 1 incident.
    build_incident(store, alarms, time_window_minutes=30)
    assert store.incident_count == 1

    store2 = RcaStore()
    # Same 8-minute gap: outside 5 → 2 incidents.
    build_incident(store2, alarms, time_window_minutes=5)
    assert store2.incident_count == 2


def test_build_incident_returns_primary_incident_for_single_group() -> None:
    """For the common single-group case, build_incident returns the incident
    directly (preserving the existing return-type contract)."""
    store = RcaStore()
    incident = build_incident(store, [_alarm("a_001")], time_window_minutes=30)
    assert incident.incident_id.startswith("inc_")
    assert incident.status == "analyzing"
