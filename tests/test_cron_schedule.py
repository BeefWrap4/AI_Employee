"""Cron-triggered scheduled runs tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from ai_employee.agent_platform_api.cron import (
    CronExpression,
    next_fire_after,
    parse_cron,
)
from ai_employee.agent_platform_api.scheduled_runs import (
    ScheduledRunStore,
    build_scheduled_run_store,
)

# --------------------------------------------------------------------------- #
# Cron parser
# --------------------------------------------------------------------------- #


def test_parse_cron_5_field() -> None:
    expr = parse_cron("*/5 * * * *")
    assert isinstance(expr, CronExpression)
    assert expr.minutes == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}


def test_parse_cron_specific_values() -> None:
    expr = parse_cron("0 9 * * 1")
    assert expr.minutes == {0}
    assert expr.hours == {9}
    assert expr.days_of_week == {1}  # Monday


def test_parse_cron_list() -> None:
    expr = parse_cron("0,15,30,45 * * * *")
    assert expr.minutes == {0, 15, 30, 45}


def test_parse_cron_range() -> None:
    expr = parse_cron("0 9-17 * * *")
    assert expr.hours == {9, 10, 11, 12, 13, 14, 15, 16, 17}


def test_parse_cron_invalid_field_count() -> None:
    with pytest.raises(ValueError):
        parse_cron("* * *")  # only 3 fields


def test_parse_cron_invalid_value() -> None:
    with pytest.raises(ValueError):
        parse_cron("60 * * * *")  # minute max is 59


def test_parse_cron_invalid_range() -> None:
    with pytest.raises(ValueError):
        parse_cron("0 25 * * *")  # hour max is 23


# --------------------------------------------------------------------------- #
# next_fire_after
# --------------------------------------------------------------------------- #


def test_next_fire_after_every_5_minutes() -> None:
    expr = parse_cron("*/5 * * * *")
    # Start at 10:03:00 → next fire is 10:05:00.
    start = datetime(2026, 6, 18, 10, 3, 0, tzinfo=timezone.utc)
    nxt = next_fire_after(expr, start)
    assert nxt == datetime(2026, 6, 18, 10, 5, 0, tzinfo=timezone.utc)


def test_next_fire_after_skips_to_next_day() -> None:
    expr = parse_cron("0 9 * * *")
    # 10:00 UTC → next is tomorrow 09:00 UTC.
    start = datetime(2026, 6, 18, 10, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_after(expr, start)
    assert nxt == datetime(2026, 6, 19, 9, 0, 0, tzinfo=timezone.utc)


def test_next_fire_after_weekday_constraint() -> None:
    expr = parse_cron("0 9 * * 1")  # Mondays only
    # 2026-06-18 is a Thursday. Next Monday is 2026-06-22.
    start = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_after(expr, start)
    assert nxt == datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc)
    assert nxt.weekday() == 0  # Monday


def test_next_fire_after_matches_immediately() -> None:
    """When ``start`` itself is a fire time, the next fire is the *next* one."""
    expr = parse_cron("0 * * * *")
    start = datetime(2026, 6, 18, 10, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_after(expr, start)
    assert nxt == datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# ScheduledRunStore
# --------------------------------------------------------------------------- #


def test_schedule_create_and_get() -> None:
    store = ScheduledRunStore()
    sched = store.create(
        template_id="rca",
        cron="*/15 * * * *",
        input={"incident_id": "inc_001"},
        requested_by="alice",
    )
    assert sched.schedule_id.startswith("sched_")
    assert sched.template_id == "rca"
    assert sched.fire_count == 0
    fetched = store.get(sched.schedule_id)
    assert fetched == sched


def test_schedule_list_returns_all() -> None:
    store = ScheduledRunStore()
    store.create(
        template_id="rca", cron="*/5 * * * *",
        input={}, requested_by="alice",
    )
    store.create(
        template_id="knowledge_qa", cron="0 9 * * *",
        input={}, requested_by="bob",
    )
    assert len(store.list_all()) == 2


def test_schedule_delete() -> None:
    store = ScheduledRunStore()
    sched = store.create(
        template_id="rca", cron="*/5 * * * *",
        input={}, requested_by="alice",
    )
    assert store.delete(sched.schedule_id) is True
    assert store.get(sched.schedule_id) is None
    assert store.delete(sched.schedule_id) is False  # second time


def test_schedule_tick_returns_due_schedules() -> None:
    store = ScheduledRunStore()
    sched = store.create(
        template_id="rca", cron="*/5 * * * *",
        input={}, requested_by="alice",
    )
    # Force a past next_fire_at to make it immediately due.
    store._schedules[sched.schedule_id].next_fire_at = (
        datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    )
    due = store.tick_due(now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc))
    assert len(due) == 1
    assert due[0].schedule_id == sched.schedule_id


def test_schedule_tick_skips_not_due() -> None:
    store = ScheduledRunStore()
    sched = store.create(
        template_id="rca", cron="*/5 * * * *",
        input={}, requested_by="alice",
    )
    # next_fire_at is in the future.
    due = store.tick_due(now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert due == []


def test_schedule_tick_advances_next_fire_after_fire() -> None:
    """After a tick fires a schedule, its next_fire_at is recomputed."""
    store = ScheduledRunStore()
    sched = store.create(
        template_id="rca", cron="*/5 * * * *",
        input={}, requested_by="alice",
    )
    initial_next = sched.next_fire_at
    store._schedules[sched.schedule_id].next_fire_at = (
        datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    )
    due = store.tick_due(now=datetime(2026, 6, 18, 10, 3, tzinfo=timezone.utc))
    assert due
    # fire_count is incremented.
    refreshed = store.get(sched.schedule_id)
    assert refreshed.fire_count == 1
    # next_fire_at is now ~10:05 on the tick date.
    assert refreshed.next_fire_at != initial_next


def test_schedule_resolve_run_id_records_history() -> None:
    store = ScheduledRunStore()
    sched = store.create(
        template_id="rca", cron="*/5 * * * *",
        input={}, requested_by="alice",
    )
    store._schedules[sched.schedule_id].next_fire_at = (
        datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    )
    due = store.tick_due(now=datetime(2026, 6, 18, 10, 3, tzinfo=timezone.utc))
    store.record_run(schedule_id=due[0].schedule_id, run_id="agent_run_001")
    refreshed = store.get(due[0].schedule_id)
    assert refreshed.recent_run_ids == ["agent_run_001"]


def test_build_scheduled_run_store_returns_singleton() -> None:
    a = build_scheduled_run_store()
    b = build_scheduled_run_store()
    assert a is b
