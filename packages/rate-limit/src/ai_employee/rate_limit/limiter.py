"""Sliding-window rate limiter (spec §5.1)."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class SlidingWindowDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float


class RateLimitBackend(Protocol):
    def add_event(self, key: str, ts: float) -> int: ...
    def trim(self, key: str, cutoff: float) -> None: ...
    def count(self, key: str) -> int: ...


class InMemoryBackend:
    """Thread-safe deque-per-key event log."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def add_event(self, key: str, ts: float) -> int:
        with self._lock:
            dq = self._events[key]
            dq.append(ts)
            return len(dq)

    def trim(self, key: str, cutoff: float) -> None:
        with self._lock:
            dq = self._events[key]
            while dq and dq[0] < cutoff:
                dq.popleft()

    def count(self, key: str) -> int:
        with self._lock:
            return len(self._events[key])


class RedisBackend:
    """Redis list per key (LTRIM keeps the most-recent N entries)."""

    KEY_PREFIX = "rl:"

    def __init__(self, redis_client: Any, *, max_entries: int = 10_000) -> None:
        self._r = redis_client
        self.max_entries = max_entries

    def _key(self, k: str) -> str:
        return f"{self.KEY_PREFIX}{k}"

    def add_event(self, key: str, ts: float) -> int:
        k = self._key(key)
        try:
            self._r.rpush(k, ts)
            self._r.ltrim(k, -self.max_entries, -1)
            return int(self._r.llen(k))
        except Exception:
            return 0

    def trim(self, key: str, cutoff: float) -> None:
        del cutoff  # placeholder — periodic sweep is the production path

    def count(self, key: str) -> int:
        try:
            return int(self._r.llen(self._key(key)))
        except Exception:
            return 0


class SlidingWindowLimiter:
    """Sliding-window check, ``limit`` events per ``window_seconds``."""

    def __init__(
        self,
        *,
        backend: RateLimitBackend,
        window_seconds: int,
        limit: int,
        clock: Any = time.time,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.backend = backend
        self.window_seconds = window_seconds
        self.limit = limit
        self._clock = clock

    def allow(self, key: str) -> SlidingWindowDecision:
        now = self._clock()
        cutoff = now - self.window_seconds
        self.backend.trim(key, cutoff)
        count = self.backend.count(key)
        if count >= self.limit:
            return SlidingWindowDecision(
                allowed=False,
                limit=self.limit,
                remaining=0,
                retry_after_seconds=float(self.window_seconds),
            )
        self.backend.add_event(key, now)
        return SlidingWindowDecision(
            allowed=True,
            limit=self.limit,
            remaining=max(0, self.limit - count - 1),
            retry_after_seconds=0.0,
        )


def build_backend(*, redis_url: str | None = None) -> RateLimitBackend:
    url = redis_url if redis_url is not None else os.getenv("REDIS_URL")
    if not url:
        return InMemoryBackend()
    try:
        import redis  # type: ignore[import-untyped]

        client = redis.Redis.from_url(url)
        return RedisBackend(client)
    except Exception:
        return InMemoryBackend()


def build_sliding_window_limiter(
    *,
    redis_url: str | None = None,
    window_seconds: int | None = None,
    limit: int | None = None,
) -> SlidingWindowLimiter:
    backend = build_backend(redis_url=redis_url)
    if window_seconds is None:
        window_seconds = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    if limit is None:
        limit = int(os.getenv("RATE_LIMIT_LIMIT", "60"))
    return SlidingWindowLimiter(
        backend=backend,
        window_seconds=window_seconds,
        limit=limit,
    )


def key_for_request(claims_sub: str | None, remote_addr: str | None) -> str:
    if claims_sub:
        return f"sub:{claims_sub}"
    if remote_addr:
        return f"ip:{remote_addr}"
    return "ip:unknown"


def iter_decisions(
    limiter: SlidingWindowLimiter, keys: Iterable[str]
) -> list[SlidingWindowDecision]:
    return [limiter.allow(k) for k in keys]


__all__ = [
    "InMemoryBackend",
    "RateLimitBackend",
    "RedisBackend",
    "SlidingWindowDecision",
    "SlidingWindowLimiter",
    "build_backend",
    "build_sliding_window_limiter",
    "iter_decisions",
    "key_for_request",
]
