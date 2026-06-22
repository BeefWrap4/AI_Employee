"""Redis-lease leader election tests.

When multiple SchedulerLoop replicas run, only the one holding the
lease ticks.  The lease is a Redis ``SET NX EX`` keyed by a fixed
leader key; the holder refreshes it before TTL expires.  When Redis is
unavailable, the loop degrades to "always leader" (single-instance
mode) so dev/test without Redis still works.
"""

from __future__ import annotations

import threading

import pytest
from ai_employee.agent_platform_api.leader_election import (
    LocalLeaderElection,
    RedisLeaderElection,
    build_leader_election,
)

# --------------------------------------------------------------------------- #
# LocalLeaderElection (no Redis)
# --------------------------------------------------------------------------- #


def test_local_leader_always_acquires() -> None:
    lease = LocalLeaderElection()
    assert lease.try_acquire() is True
    assert lease.is_leader() is True


def test_local_leader_release_is_noop() -> None:
    lease = LocalLeaderElection()
    lease.try_acquire()
    lease.release()
    # Still leader after release (single-instance semantics).
    assert lease.is_leader() is True


def test_local_leader_renew_always_true() -> None:
    lease = LocalLeaderElection()
    lease.try_acquire()
    assert lease.renew() is True


# --------------------------------------------------------------------------- #
# RedisLeaderElection with fake redis
# --------------------------------------------------------------------------- #


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


def test_redis_leader_acquire_succeeds_when_free() -> None:
    fake = _FakeRedis()
    lease = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="replica-A",
        ttl_s=10,
    )
    assert lease.try_acquire() is True
    assert lease.is_leader() is True


def test_redis_leader_acquire_fails_when_held_by_other() -> None:
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
    assert lease_a.try_acquire() is True
    assert lease_b.try_acquire() is False
    assert lease_b.is_leader() is False
    assert lease_a.is_leader() is True


def test_redis_leader_release_lets_other_acquire() -> None:
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
    lease_a.try_acquire()
    lease_a.release()
    assert lease_b.try_acquire() is True


def test_redis_leader_renew_extends_when_holder() -> None:
    fake = _FakeRedis()
    lease = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    lease.try_acquire()
    assert lease.renew() is True
    # Still leader after renew.
    assert lease.is_leader() is True


def test_redis_leader_renew_fails_when_not_holder() -> None:
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
    lease_a.try_acquire()
    # B never acquired; renew should fail.
    assert lease_b.renew() is False


def test_redis_leader_is_leader_checks_holder_id() -> None:
    """is_leader returns True only when the stored value matches our holder_id."""
    fake = _FakeRedis()
    lease_a = RedisLeaderElection(
        client=fake,
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    lease_a.try_acquire()
    # Another holder sneaks in by overwriting (simulating TTL expiry + steal).
    fake.store["leader:scheduler"] = "C"
    assert lease_a.is_leader() is False


def test_redis_leader_unreachable_redis_returns_false() -> None:
    """When Redis itself errors, acquire returns False (fail-closed)."""

    class BrokenRedis:
        def set(self, *a, **k):
            raise ConnectionError("redis down")

        def get(self, *a, **k):
            raise ConnectionError("redis down")

        def delete(self, *a, **k):
            raise ConnectionError("redis down")

    lease = RedisLeaderElection(
        client=BrokenRedis(),
        key="leader:scheduler",
        holder_id="A",
        ttl_s=10,
    )
    assert lease.try_acquire() is False
    assert lease.is_leader() is False


# --------------------------------------------------------------------------- #
# build_leader_election
# --------------------------------------------------------------------------- #


def test_build_leader_election_no_redis_returns_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    lease = build_leader_election()
    assert isinstance(lease, LocalLeaderElection)


def test_build_leader_election_unreachable_redis_returns_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("REDIS_TIMEOUT_S", "0.1")
    lease = build_leader_election()
    # Degrades to local when Redis is unreachable.
    assert isinstance(lease, LocalLeaderElection)
