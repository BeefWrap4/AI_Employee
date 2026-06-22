"""SchedulerLoop tests (cron tick loop)."""

from __future__ import annotations

import threading

from ai_employee.agent_platform_api.scheduled_runs import (
    ScheduledRun,
    ScheduledRunStore,
)
from ai_employee.agent_platform_api.scheduler_loop import (
    SchedulerLoop,
    build_scheduler_loop,
)


def _make_due_schedule(
    store: ScheduledRunStore,
    *,
    cron: str = "*/5 * * * *",
    template_id: str = "rca",
    input: dict | None = None,
) -> ScheduledRun:
    sched = store.create(
        template_id=template_id,
        cron=cron,
        input=input or {},
        requested_by="alice",
    )
    # Force it to be due now.
    store._schedules[sched.schedule_id].next_fire_at = "2020-01-01T00:00:00+00:00"
    return sched


# --------------------------------------------------------------------------- #
# run_once
# --------------------------------------------------------------------------- #


def test_run_once_invokes_callback_for_due_schedule() -> None:
    store = ScheduledRunStore()
    sched = _make_due_schedule(store)
    fired: list[ScheduledRun] = []

    loop = SchedulerLoop(store=store, fire_callback=lambda s: fired.append(s))
    due = loop.run_once()
    assert len(due) == 1
    assert fired == due
    # Schedule's next_fire_at was advanced.
    refreshed = store.get(sched.schedule_id)
    assert refreshed.next_fire_at != "2020-01-01T00:00:00+00:00"
    assert refreshed.fire_count == 1


def test_run_once_skips_when_no_due() -> None:
    store = ScheduledRunStore()
    fired: list[ScheduledRun] = []
    loop = SchedulerLoop(store=store, fire_callback=lambda s: fired.append(s))
    assert loop.run_once() == []


def test_run_once_callback_exception_does_not_break_loop() -> None:
    store = ScheduledRunStore()
    _make_due_schedule(store)

    def bad_cb(s):
        raise RuntimeError("boom")

    loop = SchedulerLoop(store=store, fire_callback=bad_cb)
    # Must not raise.
    loop.run_once()


# --------------------------------------------------------------------------- #
# Background thread lifecycle
# --------------------------------------------------------------------------- #


def test_start_runs_callback_in_background() -> None:
    store = ScheduledRunStore()
    sched = _make_due_schedule(store, cron="*/1 * * * *")
    fired: list[ScheduledRun] = []
    fired_event = threading.Event()

    def cb(s: ScheduledRun) -> None:
        fired.append(s)
        fired_event.set()

    loop = SchedulerLoop(
        store=store,
        fire_callback=cb,
        tick_interval_s=0.05,
    )
    loop.start()
    try:
        assert fired_event.wait(timeout=2.0), "callback never fired"
        assert fired
    finally:
        loop.stop()


def test_stop_halts_background_thread() -> None:
    store = ScheduledRunStore()
    loop = SchedulerLoop(
        store=store,
        fire_callback=lambda s: None,
        tick_interval_s=0.05,
    )
    loop.start()
    assert loop.is_running is True
    loop.stop()
    assert loop.is_running is False


def test_start_called_twice_is_idempotent() -> None:
    store = ScheduledRunStore()
    loop = SchedulerLoop(store=store, fire_callback=lambda s: None, tick_interval_s=0.05)
    loop.start()
    first_thread = loop._thread
    loop.start()  # second call should be a no-op
    assert loop._thread is first_thread
    loop.stop()


def test_build_scheduler_loop_returns_singleton() -> None:
    a = build_scheduler_loop()
    b = build_scheduler_loop()
    assert a is b


def test_loop_records_run_id_via_callback_metadata() -> None:
    """The callback can return a run_id; the loop records it on the schedule."""
    store = ScheduledRunStore()
    sched = _make_due_schedule(store)

    def cb(s: ScheduledRun) -> str:  # type: ignore[return-value]
        return "agent_run_test_001"

    loop = SchedulerLoop(store=store, fire_callback=cb, auto_record_runs=True)
    loop.run_once()
    refreshed = store.get(sched.schedule_id)
    assert refreshed.recent_run_ids == ["agent_run_test_001"]


def test_loop_without_auto_record_does_not_record() -> None:
    store = ScheduledRunStore()
    sched = _make_due_schedule(store)

    def cb(s: ScheduledRun) -> str:  # type: ignore[return-value]
        return "agent_run_ignored"

    loop = SchedulerLoop(store=store, fire_callback=cb, auto_record_runs=False)
    loop.run_once()
    refreshed = store.get(sched.schedule_id)
    assert refreshed.recent_run_ids == []
