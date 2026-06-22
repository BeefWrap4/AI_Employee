"""Sliding-window rate limiter tests (spec §5.1).

A sliding-window counter persists per-key timestamps in Redis (or in an
in-memory dict when Redis is unavailable).  The check:

    sum over the last W seconds  <  limit  ⇒  allow

A retry-after seconds value is reported when the bucket is full.
"""

from __future__ import annotations

import time

from ai_employee.agent_platform_api.rate_limit_redis import (
    InMemoryBackend,
    RedisBackend,
    SlidingWindowLimiter,
)


def _backend() -> InMemoryBackend:
    return InMemoryBackend()


def test_sliding_window_allows_under_limit() -> None:
    limiter = SlidingWindowLimiter(
        backend=_backend(),
        window_seconds=60,
        limit=3,
    )
    for _ in range(3):
        decision = limiter.allow("user-1")
        assert decision.allowed is True


def test_sliding_window_blocks_over_limit() -> None:
    limiter = SlidingWindowLimiter(
        backend=_backend(),
        window_seconds=60,
        limit=3,
    )
    for _ in range(3):
        limiter.allow("user-1")
    decision = limiter.allow("user-1")
    assert decision.allowed is False
    assert decision.retry_after_seconds > 0


def test_sliding_window_recovers_after_window() -> None:
    """Once events fall outside the window, the bucket drains."""
    backend = _backend()
    limiter = SlidingWindowLimiter(
        backend=backend,
        window_seconds=1,
        limit=2,
    )
    for _ in range(2):
        limiter.allow("user-1")
    assert limiter.allow("user-1").allowed is False
    # Wait for the window to elapse.
    time.sleep(1.1)
    decision = limiter.allow("user-1")
    assert decision.allowed is True


def test_sliding_window_separate_keys_independent() -> None:
    limiter = SlidingWindowLimiter(
        backend=_backend(),
        window_seconds=60,
        limit=2,
    )
    for _ in range(2):
        limiter.allow("user-1")
    assert limiter.allow("user-1").allowed is False
    # Different key starts fresh.
    assert limiter.allow("user-2").allowed is True


def test_in_memory_backend_is_thread_safe() -> None:
    """A burst from many callers only grants `limit` permits."""
    limiter = SlidingWindowLimiter(
        backend=_backend(),
        window_seconds=60,
        limit=10,
    )
    from threading import Thread

    results: list[bool] = []
    lock_ = []

    def hit() -> None:
        d = limiter.allow("shared")
        results.append(d.allowed)

    threads = [Thread(target=hit) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    allowed = sum(1 for r in results if r)
    assert allowed == 10


def test_redis_backend_factory_returns_in_memory_when_unset() -> None:
    """Without REDIS_URL the factory falls back to the in-memory backend."""
    from ai_employee.agent_platform_api.rate_limit_redis import build_backend

    backend = build_backend(redis_url=None)
    assert isinstance(backend, InMemoryBackend)


def test_redis_backend_uses_redis_when_available() -> None:
    """A fake Redis client is wrapped by ``RedisBackend``."""
    fake = type(
        "R",
        (),
        {
            "incr": lambda self, k: 1,
            "expire": lambda self, k, t: True,
            "lrem": lambda self, k, n, v: 1,
            "rpush": lambda self, k, v: 1,
            "ltrim": lambda self, k, s, e: True,
            "llen": lambda self, k: 1,
        },
    )()
    backend = RedisBackend(fake)  # type: ignore[arg-type]
    limiter = SlidingWindowLimiter(
        backend=backend,
        window_seconds=60,
        limit=5,
    )
    # First hit goes through the fake — verify it doesn't crash.
    decision = limiter.allow("user-1")
    assert decision is not None


def test_sliding_window_retry_after_is_positive_when_blocked() -> None:
    limiter = SlidingWindowLimiter(
        backend=_backend(),
        window_seconds=10,
        limit=1,
    )
    limiter.allow("user-1")
    decision = limiter.allow("user-1")
    assert decision.allowed is False
    assert 0 < decision.retry_after_seconds <= 10


def test_sliding_window_emits_remaining_header() -> None:
    """Even when allowed, the response carries remaining-capacity info."""
    limiter = SlidingWindowLimiter(
        backend=_backend(),
        window_seconds=60,
        limit=5,
    )
    decision = limiter.allow("user-1")
    assert decision.allowed is True
    assert decision.remaining == 4
    assert decision.limit == 5
