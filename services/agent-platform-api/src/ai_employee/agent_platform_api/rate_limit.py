"""Token-bucket rate limiter middleware (spec §5.1).

Per-key (subject or remote IP) fixed-window limiter with optional
burst capacity.  In-memory, thread-safe.  Use :func:`build_limiter` to
obtain an instance and :func:`check` (or :meth:`TokenBucketLimiter.allow`)
on every request.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class RateLimitDecision:
    allowed: bool
    remaining: int
    reset_seconds: float


class TokenBucketLimiter:
    """Thread-safe token-bucket per-key rate limiter."""

    def __init__(self, *, rate_per_minute: int, burst: int) -> None:
        self.rate = rate_per_minute
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _refill(self, key: str, now: float) -> float:
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        # Refill at `rate` tokens per minute.
        elapsed = max(0.0, now - last)
        refill = elapsed * (self.rate / 60.0)
        tokens = min(self.burst, tokens + refill)
        self._buckets[key] = (tokens, now)
        return tokens

    def allow(self, key: str) -> RateLimitDecision:
        now = time.monotonic()
        with self._lock:
            tokens = self._refill(key, now)
            if tokens >= 1.0:
                tokens -= 1.0
                self._buckets[key] = (tokens, now)
                reset = (self.burst - tokens) / max(self.rate / 60.0, 1e-9)
                return RateLimitDecision(allowed=True, remaining=int(tokens), reset_seconds=reset)
            reset = (1.0 - tokens) / max(self.rate / 60.0, 1e-9)
            return RateLimitDecision(allowed=False, remaining=0, reset_seconds=reset)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


def build_limiter() -> TokenBucketLimiter:
    """Build a limiter from env vars; returns a permissive fallback when disabled."""
    if os.getenv("RATE_LIMIT_ENABLED", "false").strip().lower() != "true":
        return _DisabledLimiter()  # type: ignore[return-value]
    return TokenBucketLimiter(
        rate_per_minute=int(os.getenv("RATE_LIMIT_RPM", "60")),
        burst=int(os.getenv("RATE_LIMIT_BURST", "10")),
    )


class _DisabledLimiter(TokenBucketLimiter):
    def __init__(self) -> None:  # noqa: D401 - sentinel class
        super().__init__(rate_per_minute=10_000_000, burst=10_000_000)

    def allow(self, key: str) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, remaining=10_000_000, reset_seconds=0.0)


def key_for_request(claims_sub: str | None, remote_addr: str | None) -> str:
    """Pick a stable key from the request — subject if known, else IP."""
    if claims_sub:
        return f"sub:{claims_sub}"
    if remote_addr:
        return f"ip:{remote_addr}"
    return "ip:unknown"


__all__ = [
    "RateLimitDecision",
    "TokenBucketLimiter",
    "build_limiter",
    "key_for_request",
]


def rate_limit_dependency(
    limiter: TokenBucketLimiter,
) -> Callable[[str | None, str | None], RateLimitDecision]:
    """Return a FastAPI dependency factory bound to ``limiter``."""

    def _dep(
        claims_sub: str | None = None,
        remote_addr: str | None = None,
    ) -> RateLimitDecision:
        key = key_for_request(claims_sub, remote_addr)
        return limiter.allow(key)

    return _dep
