"""Idempotency-Key store tests (R23).

A request that carries an ``Idempotency-Key`` header must be executed
exactly once even when retried (client timeout + retry, or a load
balancer replaying a request to a different replica).  The store
exposes ``get_or_begin`` (claim the key, returning an in-flight marker
or a cached completed result) and ``complete`` (store the result).  A
second caller hitting the same key while the first is in flight gets
the cached result once the first finishes.

Backends:
* :class:`InMemoryIdempotencyStore` — dict, single process.
* :class:`RedisIdempotencyStore` — Redis hash + TTL, multi-replica.
"""

from __future__ import annotations

import threading
import time

import pytest
from ai_employee.common_schemas.idempotency import (
    IdempotencyRecord,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
    build_idempotency_store,
)

# --------------------------------------------------------------------------- #
# InMemoryIdempotencyStore
# --------------------------------------------------------------------------- #


def test_first_call_begins_in_flight() -> None:
    store = InMemoryIdempotencyStore()
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"
    assert rec.result is None


def test_complete_stores_result() -> None:
    store = InMemoryIdempotencyStore()
    store.get_or_begin("k1")
    store.complete("k1", status="success", result={"run_id": "r1"})
    rec = store.get_or_begin("k1")
    assert rec.status == "success"
    assert rec.result == {"run_id": "r1"}


def test_second_call_returns_cached_result() -> None:
    store = InMemoryIdempotencyStore()
    store.get_or_begin("k1")
    store.complete("k1", status="success", result={"run_id": "r1"})
    rec = store.get_or_begin("k1")
    assert rec.status == "success"
    assert rec.result == {"run_id": "r1"}


def test_in_flight_second_call_returns_in_flight_marker() -> None:
    """A concurrent caller sees status=in_flight (no result yet)."""
    store = InMemoryIdempotencyStore()
    store.get_or_begin("k1")
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"
    assert rec.result is None


def test_failed_result_is_cached_so_retries_dont_re_execute() -> None:
    """A failed attempt is also cached to avoid retry storms."""
    store = InMemoryIdempotencyStore()
    store.get_or_begin("k1")
    store.complete("k1", status="failed", result={"error": "boom"})
    rec = store.get_or_begin("k1")
    assert rec.status == "failed"
    assert rec.result == {"error": "boom"}


def test_distinct_keys_are_independent() -> None:
    store = InMemoryIdempotencyStore()
    store.get_or_begin("k1")
    store.complete("k1", status="success", result={"run_id": "r1"})
    rec2 = store.get_or_begin("k2")
    assert rec2.status == "in_flight"


def test_complete_unknown_key_is_noop() -> None:
    store = InMemoryIdempotencyStore()
    # Completing a key that was never begun must not raise.
    store.complete("missing", status="success", result={})


def test_expired_records_are_evicted() -> None:
    """Records older than ttl_s are treated as absent (re-executable)."""
    store = InMemoryIdempotencyStore(ttl_s=0.05)
    store.get_or_begin("k1")
    store.complete("k1", status="success", result={"run_id": "r1"})
    time.sleep(0.06)
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"


# --------------------------------------------------------------------------- #
# RedisIdempotencyStore with a fake redis
# --------------------------------------------------------------------------- #


class _FakeRedis:
    """Minimal thread-safe fake covering HSET/HGET/HGETALL/EXPIRE/SET NX."""

    def __init__(self) -> None:
        self.hash_store: dict[str, dict[str, str]] = {}
        self.kv: dict[str, str] = {}
        self._lock = threading.Lock()

    def hset(self, name: str, key: str, value: str) -> int:
        with self._lock:
            self.hash_store.setdefault(name, {})[key] = value
            return 1

    def hget(self, name: str, key: str) -> str | None:
        with self._lock:
            return self.hash_store.get(name, {}).get(key)

    def hgetall(self, name: str) -> dict[str, str]:
        with self._lock:
            return dict(self.hash_store.get(name, {}))

    def expire(self, name: str, ttl: int) -> bool:
        # Fake does not honour TTL expiry in real time; tests that need
        # eviction call .delete explicitly.
        return name in self.hash_store or name in self.kv

    def delete(self, *names: str) -> int:
        with self._lock:
            n = 0
            for name in names:
                if name in self.hash_store:
                    n += 1
                    del self.hash_store[name]
                if name in self.kv:
                    n += 1
                    del self.kv[name]
            return n

    def set(self, name: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        with self._lock:
            if nx and name in self.kv:
                return False
            self.kv[name] = value
            return True

    def get(self, name: str) -> str | None:
        with self._lock:
            return self.kv.get(name)


def test_redis_store_begin_then_complete() -> None:
    fake = _FakeRedis()
    store = RedisIdempotencyStore(client=fake, key_prefix="idem:")
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"
    store.complete("k1", status="success", result={"run_id": "r1"})
    rec2 = store.get_or_begin("k1")
    assert rec2.status == "success"
    assert rec2.result == {"run_id": "r1"}


def test_redis_store_in_flight_second_call_returns_marker() -> None:
    fake = _FakeRedis()
    store = RedisIdempotencyStore(client=fake, key_prefix="idem:")
    store.get_or_begin("k1")
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"


def test_redis_store_distinct_keys_independent() -> None:
    fake = _FakeRedis()
    store = RedisIdempotencyStore(client=fake, key_prefix="idem:")
    store.get_or_begin("k1")
    store.complete("k1", status="success", result={"run_id": "r1"})
    rec2 = store.get_or_begin("k2")
    assert rec2.status == "in_flight"


def test_redis_store_evicts_when_key_deleted() -> None:
    fake = _FakeRedis()
    store = RedisIdempotencyStore(client=fake, key_prefix="idem:")
    store.get_or_begin("k1")
    store.complete("k1", status="success", result={"run_id": "r1"})
    fake.delete("idem:k1")
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"


def test_redis_store_redis_error_returns_in_flight() -> None:
    """When Redis itself errors, the store fails open (re-executes)."""

    class BrokenRedis:
        def hgetall(self, *a, **k):
            raise ConnectionError("redis down")

        def hset(self, *a, **k):
            raise ConnectionError("redis down")

        def hget(self, *a, **k):
            raise ConnectionError("redis down")

        def expire(self, *a, **k):
            return False

    store = RedisIdempotencyStore(client=BrokenRedis(), key_prefix="idem:")
    rec = store.get_or_begin("k1")
    assert rec.status == "in_flight"


# --------------------------------------------------------------------------- #
# build_idempotency_store
# --------------------------------------------------------------------------- #


def test_build_store_no_redis_returns_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = build_idempotency_store()
    assert isinstance(store, InMemoryIdempotencyStore)


def test_build_store_redis_unreachable_returns_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    store = build_idempotency_store()
    assert isinstance(store, InMemoryIdempotencyStore)


def test_idempotency_record_to_dict_roundtrip() -> None:
    rec = IdempotencyRecord(key="k1", status="success", result={"a": 1})
    d = rec.to_dict()
    assert d["status"] == "success"
    assert d["result"] == {"a": 1}
    assert d["key"] == "k1"


def test_store_satisfies_protocol() -> None:
    """Both backends implement the IdempotencyStore protocol."""
    mem = InMemoryIdempotencyStore()
    red = RedisIdempotencyStore(client=_FakeRedis(), key_prefix="idem:")
    assert isinstance(mem, IdempotencyStore)
    assert isinstance(red, IdempotencyStore)
