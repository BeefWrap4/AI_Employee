"""HA leader-failover regression test (R23-5).

When N agent-platform-api replicas run, only the one holding the Redis
leader lease ticks the scheduler.  This test simulates a two-replica
deployment with a shared fake-Redis lease and asserts:

1. Only the leader ticks (the standby's ``run_once`` is a no-op).
2. After the leader releases the lease (crash / shutdown), the standby
   acquires it on its next tick — no schedule is double-fired and no
   tick is lost.

This is the regression guard for the multi-replica scheduler safety
described in ``infra/helm/HA.md``.
"""

from __future__ import annotations

import threading

from ai_employee.agent_platform_api.leader_election import (
    RedisLeaderElection,
)
from ai_employee.agent_platform_api.scheduled_runs import (
    ScheduledRun,
    ScheduledRunStore,
)
from ai_employee.agent_platform_api.scheduler_loop import SchedulerLoop


class _FakeRedis:
    """Minimal fake: supports SET NX EX, GET, DEL, SET (overwrite)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        with self._lock:
            if nx and key in self.store:
                return False
            self.store[key] = value
            return True

    def get(self, key: str) -> str | None:
        with self._lock:
            return self.store.get(key)

    def delete(self, key: str) -> int:
        with self._lock:
            existed = key in self.store
            self.store.pop(key, None)
            return 1 if existed else 0


def _make_due_schedule(store: ScheduledRunStore) -> ScheduledRun:
    sched = store.create(
        template_id="rca",
        cron="*/5 * * * *",
        input={},
        requested_by="alice",
    )
    store._schedules[sched.schedule_id].next_fire_at = "2020-01-01T00:00:00+00:00"
    return sched


def test_only_leader_ticks_when_two_replicas_share_lease() -> None:
    """Two SchedulerLoops, one shared lease → only the leader fires."""
    fake = _FakeRedis()
    lease_a = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    lease_b = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="B",
        ttl_s=10,
    )
    store_a = ScheduledRunStore()
    store_b = ScheduledRunStore()
    # Both replicas see the same due schedule (in HA they'd share the
    # Postgres-backed store; here we mirror it manually).
    sched_a = _make_due_schedule(store_a)
    sched_b = _make_due_schedule(store_b)

    fired_a: list[ScheduledRun] = []
    fired_b: list[ScheduledRun] = []
    loop_a = SchedulerLoop(
        store=store_a,
        fire_callback=lambda s: fired_a.append(s),
        leader_lease=lease_a,
    )
    loop_b = SchedulerLoop(
        store=store_b,
        fire_callback=lambda s: fired_b.append(s),
        leader_lease=lease_b,
    )

    # A acquires first.
    assert lease_a.try_acquire() is True
    # B cannot acquire while A holds the lease.
    assert lease_b.try_acquire() is False

    # Only A ticks; B's run_once returns [] (not leader).
    due_a = loop_a.run_once()
    due_b = loop_b.run_once()
    assert len(due_a) == 1
    assert due_b == []
    assert len(fired_a) == 1
    assert fired_b == []
    # The schedule was advanced on A, not on B.
    assert store_a.get(sched_a.schedule_id).fire_count == 1
    # B's copy is still due (unticked).
    assert store_b.get(sched_b.schedule_id).fire_count == 0


def test_standby_takes_over_after_leader_releases() -> None:
    """Leader crash (release) → standby acquires and ticks, no double-fire."""
    fake = _FakeRedis()
    lease_a = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    lease_b = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="B",
        ttl_s=10,
    )
    store_a = ScheduledRunStore()
    store_b = ScheduledRunStore()
    _make_due_schedule(store_a)
    sched_b = _make_due_schedule(store_b)

    fired_a: list[ScheduledRun] = []
    fired_b: list[ScheduledRun] = []
    loop_a = SchedulerLoop(
        store=store_a,
        fire_callback=lambda s: fired_a.append(s),
        leader_lease=lease_a,
    )
    loop_b = SchedulerLoop(
        store=store_b,
        fire_callback=lambda s: fired_b.append(s),
        leader_lease=lease_b,
    )

    # A is leader, ticks once, then "crashes" (releases the lease).
    lease_a.try_acquire()
    loop_a.run_once()
    assert len(fired_a) == 1
    lease_a.release()
    # A is no longer leader after release.
    assert lease_a.is_leader() is False

    # B's next tick acquires the now-free lease and fires the schedule.
    due_b = loop_b.run_once()
    assert len(due_b) == 1
    assert len(fired_b) == 1
    assert lease_b.is_leader() is True

    # No double-fire: A fired once, B fired once — total 1 per replica,
    # and B's store copy advanced exactly once.
    assert store_b.get(sched_b.schedule_id).fire_count == 1


def test_standby_takes_over_after_lease_stolen_via_ttl_expiry() -> None:
    """Lease overwritten (simulated TTL expiry) → old leader stops ticking."""
    fake = _FakeRedis()
    lease_a = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    lease_b = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="B",
        ttl_s=10,
    )
    store_a = ScheduledRunStore()
    store_b = ScheduledRunStore()
    _make_due_schedule(store_a)
    _make_due_schedule(store_b)

    fired_a: list[ScheduledRun] = []
    fired_b: list[ScheduledRun] = []
    loop_a = SchedulerLoop(
        store=store_a,
        fire_callback=lambda s: fired_a.append(s),
        leader_lease=lease_a,
    )
    loop_b = SchedulerLoop(
        store=store_b,
        fire_callback=lambda s: fired_b.append(s),
        leader_lease=lease_b,
    )

    lease_a.try_acquire()
    loop_a.run_once()
    # Simulate TTL expiry: the lease key disappears (Redis evicted it),
    # so B's next SET NX succeeds.
    fake.delete("leader:scheduler")
    assert lease_b.try_acquire() is True
    # A is no longer leader — its next run_once must be a no-op.
    due_a = loop_a.run_once()
    assert due_a == []
    # The schedule was not double-fired on A in this second tick.
    assert len(fired_a) == 1
    # B, now leader, ticks.
    due_b = loop_b.run_once()
    assert len(due_b) == 1
    assert len(fired_b) == 1


def test_renew_keeps_leadership_between_ticks() -> None:
    """The leader keeps ticking without re-acquiring each time."""
    fake = _FakeRedis()
    lease = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    store = ScheduledRunStore()
    _make_due_schedule(store)
    fired: list[ScheduledRun] = []
    loop = SchedulerLoop(
        store=store,
        fire_callback=lambda s: fired.append(s),
        leader_lease=lease,
        renew_ratio=1.0,  # renew every tick
    )
    lease.try_acquire()
    # First tick: due schedule fires.
    assert len(loop.run_once()) == 1
    # Second tick: lease renewed, still leader, no due schedule left.
    assert loop.run_once() == []
    assert lease.is_leader() is True
    assert len(fired) == 1
