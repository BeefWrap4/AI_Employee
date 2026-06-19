"""Idempotency-Key store (spec §5.x / R23 HA).

Lets a stateless API fronted by N replicas honour an ``Idempotency-Key``
header: a retried request (client timeout + replay, or a load-balancer
redirect to another replica) is executed exactly once.  The store
tracks each key's status — ``in_flight`` while the first request is
running, then ``success`` / ``failed`` once it finishes — and caches
the result so the retry returns the original response verbatim.

Backends:

* :class:`InMemoryIdempotencyStore` — dict + lock, single process.
  Suitable for tests and single-replica dev.  Multi-replica deployments
  must use the Redis backend so all replicas share one key namespace.
* :class:`RedisIdempotencyStore` — Redis hash per key with a TTL, so a
  cached result is visible to every replica and expires automatically.

:func:`build_idempotency_store` picks one from env: ``REDIS_URL`` (set
→ Redis, with graceful fallback to in-memory when Redis is unreachable).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class IdempotencyRecord:
    """One key's lifecycle state."""

    key: str
    status: str  # "in_flight" | "success" | "failed"
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
        }


@runtime_checkable
class IdempotencyStore(Protocol):
    """Contract every idempotency backend satisfies."""

    def get_or_begin(self, key: str) -> IdempotencyRecord:
        """Return the existing record for ``key`` or claim it in-flight.

        * If the key is unknown (or its TTL has expired), mark it
          ``in_flight`` and return that record — the caller now owns
          the execution.
        * If the key is already ``in_flight``, return the marker so the
          caller can short-circuit (the first caller will publish the
          result shortly).
        * If the key is ``success`` / ``failed``, return the cached
          record so the caller can replay the original response.
        """
        ...

    def complete(self, key: str, *, status: str, result: dict[str, Any] | None) -> None:
        """Record the terminal result for ``key``.

        No-op when the key was never begun (defensive against stray
        completions).  ``status`` must be ``success`` or ``failed``.
        """
        ...


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #


class InMemoryIdempotencyStore:
    """Dict-backed idempotency store (single process)."""

    def __init__(self, *, ttl_s: float = 86400.0) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()
        self._ttl_s = float(ttl_s)

    def _evict_expired(self, key: str, now: float) -> None:
        rec = self._records.get(key)
        if rec is None:
            return
        if now - rec.created_at > self._ttl_s:
            self._records.pop(key, None)

    def get_or_begin(self, key: str) -> IdempotencyRecord:
        now = time.time()
        with self._lock:
            self._evict_expired(key, now)
            rec = self._records.get(key)
            if rec is not None:
                return rec
            rec = IdempotencyRecord(key=key, status="in_flight", result=None)
            self._records[key] = rec
            return rec

    def complete(self, key: str, *, status: str, result: dict[str, Any] | None) -> None:
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                # Defensive: completing a key that was never begun.
                return
            existing.status = status
            existing.result = result


# --------------------------------------------------------------------------- #
# Redis backend
# --------------------------------------------------------------------------- #


class RedisIdempotencyStore:
    """Redis hash per key, multi-replica safe.

    The record is stored as a Redis hash with fields ``status``,
    ``result`` (JSON), and ``created_at``.  The whole hash gets a TTL
    so stale records expire without a sweeper.  When Redis is
    unreachable, :meth:`get_or_begin` fails open by returning a fresh
    ``in_flight`` record — the request is re-executed, which is the
    safe (if not strictly idempotent) behaviour for a transient outage.
    """

    def __init__(
        self,
        *,
        client: Any,
        key_prefix: str = "idem:",
        ttl_s: int = 86400,
    ) -> None:
        self._r = client
        self._prefix = key_prefix
        self._ttl_s = max(1, int(ttl_s))

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get_or_begin(self, key: str) -> IdempotencyRecord:
        rkey = self._key(key)
        try:
            data = self._r.hgetall(rkey)
        except Exception as exc:
            logger.warning("idempotency hgetall failed for %s: %s", key, exc)
            return IdempotencyRecord(key=key, status="in_flight", result=None)
        if data:
            status = (
                data.get("status", b"in_flight")
                if not isinstance(data.get("status"), str)
                else data.get("status", "in_flight")
            )
            if isinstance(status, bytes):
                status = status.decode("utf-8", errors="replace")
            raw_result = data.get("result")
            if isinstance(raw_result, bytes):
                raw_result = raw_result.decode("utf-8", errors="replace")
            result = json.loads(raw_result) if raw_result else None
            return IdempotencyRecord(key=key, status=status, result=result)
        # Claim in-flight.
        try:
            self._r.hset(rkey, "status", "in_flight")
            self._r.hset(rkey, "result", "")
            self._r.hset(rkey, "created_at", str(time.time()))
            self._r.expire(rkey, self._ttl_s)
        except Exception as exc:
            logger.warning("idempotency hset failed for %s: %s", key, exc)
        return IdempotencyRecord(key=key, status="in_flight", result=None)

    def complete(self, key: str, *, status: str, result: dict[str, Any] | None) -> None:
        rkey = self._key(key)
        try:
            self._r.hset(rkey, "status", status)
            self._r.hset(rkey, "result", json.dumps(result) if result is not None else "")
            self._r.expire(rkey, self._ttl_s)
        except Exception as exc:
            logger.warning("idempotency complete failed for %s: %s", key, exc)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def _connect_redis(url: str, *, timeout_s: float) -> Any:
    import redis  # type: ignore[import-not-found]

    return redis.Redis.from_url(url, socket_timeout=timeout_s)


def build_idempotency_store(
    *, redis_url: str | None = None, ttl_s: int = 86400
) -> IdempotencyStore:
    """Build an idempotency store from env.

    Returns :class:`InMemoryIdempotencyStore` when ``REDIS_URL`` is
    unset or Redis is unreachable, so single-replica deployments and
    tests keep working without a Redis server.
    """
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    if not url:
        return InMemoryIdempotencyStore(ttl_s=ttl_s)
    try:
        timeout = float(os.environ.get("REDIS_TIMEOUT_S", "0.5"))
        client = _connect_redis(url, timeout_s=timeout)
        client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable for idempotency store: %s", exc)
        return InMemoryIdempotencyStore(ttl_s=ttl_s)
    return RedisIdempotencyStore(client=client, ttl_s=ttl_s)


__all__ = [
    "IdempotencyRecord",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "RedisIdempotencyStore",
    "build_idempotency_store",
]
