"""Spec §6.2 advanced convergence rules (parent-child + topology).

Extends :mod:`test_alarm_correlation` with two more passes beyond
``time_window_minutes``:

1. **Parent-child rule** — when a child alarm (e.g. ``RRC_SETUP_FAIL``)
   appears within ``child_lag_seconds`` of a parent (e.g. ``LINK_DEGRADE``)
   *and* on the same site/cell, merge into the parent's group regardless
   of how far apart in time they are.

2. **Topology rule** — when ``upstream_site_ids`` is supplied, alarms on
   UPSTREAM/NEIGHBOR sites within ``topology_window_minutes`` of the
   primary are absorbed into the same incident (site-correlation across
   the network).

These let the incident reflect the real root-cause cluster (a link outage
on an upstream switch pulling alarms on dependent cells).
"""
from __future__ import annotations

from typing import Any

from ai_employee.rca_agent.runtime import RcaStore, build_incident
from ai_employee.rca_agent.schemas import RawAlarmEvent

# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #


def _alarm(
    alarm_id: str,
    *,
    code: str = "LINK_DEGRADE",
    site: str = "SITE-001",
    ne: str = "NE-001",
    cell: str | None = "CELL-001",
    severity: str = "major",
    start: str = "2026-06-17T10:00:00+08:00",
    parent: str | None = None,
    upstream_site_ids: list[str] | None = None,
) -> RawAlarmEvent:
    payload: dict[str, Any] = {}
    if parent is not None:
        payload["parent_alarm_id"] = parent
    if upstream_site_ids is not None:
        payload["upstream_site_ids"] = upstream_site_ids
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
        raw_payload=payload,
    )


# --------------------------------------------------------------------------- #
# Parent-child rule
# --------------------------------------------------------------------------- #


def test_parent_child_alarms_merge_into_parent_group() -> None:
    """A child alarm (RRC_SETUP_FAIL) following a parent (LINK_DEGRADE)
    on the same site/cell merges into the parent's incident even when
    the gap is much larger than time_window_minutes."""
    store = RcaStore()
    alarms = [
        _alarm(
            "parent_001", code="LINK_DEGRADE", severity="critical",
            start="2026-06-17T10:00:00+08:00",
        ),
        _alarm(
            "child_001", code="RRC_SETUP_FAIL", severity="major",
            start="2026-06-17T10:25:00+08:00",
            parent="parent_001",
        ),
    ]
    # 25-min gap → outside a 5-min window but parent-child rule merges
    # because the lag is set wide enough (1800s = 30 min) to absorb the gap.
    incident = build_incident(
        store, alarms, time_window_minutes=5,
        topology_window_minutes=120,
        parent_child_lag_seconds=1800,
    )
    assert store.incident_count == 1
    assert len(incident.alarm_events) == 2
    # Parent remains primary (highest severity, earliest time).
    assert incident.primary_alarm.alarm_id == "parent_001"


def test_parent_child_does_not_merge_across_sites() -> None:
    """A child alarm on a *different* site is not pulled in even if the
    alarm_id references the parent."""
    store = RcaStore()
    alarms = [
        _alarm(
            "parent_001", site="SITE-001", code="LINK_DEGRADE",
            severity="critical", start="2026-06-17T10:00:00+08:00",
        ),
        _alarm(
            "child_001", site="SITE-002", code="RRC_SETUP_FAIL",
            start="2026-06-17T10:05:00+08:00", parent="parent_001",
        ),
    ]
    incident = build_incident(
        store, alarms, time_window_minutes=5,
        topology_window_minutes=120,
    )
    # Different site → two separate incidents.
    assert store.incident_count == 2


def test_parent_child_rule_disabled_when_set_to_zero() -> None:
    """``parent_child_lag_seconds=0`` disables the rule entirely."""
    store = RcaStore()
    alarms = [
        _alarm(
            "parent_001", code="LINK_DEGRADE", severity="critical",
            start="2026-06-17T10:00:00+08:00",
        ),
        _alarm(
            "child_001", code="RRC_SETUP_FAIL", severity="major",
            start="2026-06-17T10:25:00+08:00", parent="parent_001",
        ),
    ]
    build_incident(
        store, alarms, time_window_minutes=5,
        parent_child_lag_seconds=0,
        topology_window_minutes=120,
    )
    assert store.incident_count == 2


# --------------------------------------------------------------------------- #
# Topology rule (UPSTREAM / NEIGHBOR correlation)
# --------------------------------------------------------------------------- #


def test_topology_rule_merges_upstream_site_alarms() -> None:
    """An alarm on an upstream site within the topology window merges
    with the primary's incident via the upstream_site_ids correlation."""
    store = RcaStore()
    alarms = [
        _alarm(
            "down_001", code="RRC_SETUP_FAIL", site="SITE-CELL",
            severity="major", start="2026-06-17T10:00:00+08:00",
            upstream_site_ids=["SITE-AGG"],
        ),
        _alarm(
            "up_001", code="LINK_DEGRADE", site="SITE-AGG",
            severity="critical", start="2026-06-17T10:02:00+08:00",
        ),
    ]
    incident = build_incident(
        store, alarms, time_window_minutes=5,
        topology_window_minutes=30,
    )
    # Topology rule binds the upstream alarm into the downstream's incident.
    assert store.incident_count == 1
    assert len(incident.alarm_events) == 2
    # Critical upstream is primary.
    assert incident.primary_alarm.alarm_id == "up_001"


def test_topology_rule_does_not_merge_outside_window() -> None:
    """An upstream alarm more than topology_window_minutes away stays separate."""
    store = RcaStore()
    alarms = [
        _alarm(
            "down_001", site="SITE-CELL", start="2026-06-17T10:00:00+08:00",
            upstream_site_ids=["SITE-AGG"],
        ),
        _alarm(
            "up_001", site="SITE-AGG", start="2026-06-17T10:45:00+08:00",
        ),
    ]
    build_incident(
        store, alarms, time_window_minutes=5,
        topology_window_minutes=30,
    )
    assert store.incident_count == 2


def test_topology_rule_disabled_when_window_zero() -> None:
    store = RcaStore()
    alarms = [
        _alarm(
            "down_001", site="SITE-CELL", upstream_site_ids=["SITE-AGG"],
            start="2026-06-17T10:00:00+08:00",
        ),
        _alarm(
            "up_001", site="SITE-AGG", start="2026-06-17T10:02:00+08:00",
        ),
    ]
    build_incident(
        store, alarms, time_window_minutes=5,
        topology_window_minutes=0,
    )
    assert store.incident_count == 2


# --------------------------------------------------------------------------- #
# Rule combination: all three passes
# --------------------------------------------------------------------------- #


def test_combined_passes_compose_cleanly() -> None:
    """Three alarms at the same site within a small window → one incident
    (temporal), parent-child + topology both contribute but don't fight."""
    store = RcaStore()
    alarms = [
        _alarm(
            "a_root", code="LINK_DEGRADE", severity="critical",
            start="2026-06-17T10:00:00+08:00",
            upstream_site_ids=["SITE-AGG"],
        ),
        _alarm(
            "a_child", code="RRC_SETUP_FAIL", severity="major",
            start="2026-06-17T10:03:00+08:00", parent="a_root",
        ),
        _alarm(
            "a_up", code="LINK_LOS", site="SITE-AGG",
            severity="critical", start="2026-06-17T10:01:00+08:00",
        ),
    ]
    incident = build_incident(
        store, alarms, time_window_minutes=30,
        topology_window_minutes=30,
        parent_child_lag_seconds=600,
    )
    assert store.incident_count == 1
    assert len(incident.alarm_events) == 3
