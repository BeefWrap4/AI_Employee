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
from collections.abc import Callable
from dataclasses import dataclass


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
    def __init__(self) -> None:
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
    "PerTemplateLimiter",
    "RateLimitDecision",
    "TokenBucketLimiter",
    "build_limiter",
    "key_for_request",
    "parse_template_rate_limit_env",
    "template_key_for_request",
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



def parse_template_rate_limit_env(raw: str) -> dict[str, tuple[int, int]]:
    """Parse RATE_LIMIT_PER_TEMPLATE env value.

    Format: template_id:rate_per_minute,burst;template_id:rate,burst.
    Malformed segments are skipped silently so a typo in one template's
    quota doesn't disable the whole limiter.
    """
    out: dict[str, tuple[int, int]] = {}
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if ":" not in segment or "," not in segment:
            continue
        template_id, rates = segment.split(":", 1)
        template_id = template_id.strip()
        if not template_id:
            continue
        parts = [p.strip() for p in rates.split(",")]
        if len(parts) != 2:
            continue
        try:
            rate = int(parts[0])
            burst = int(parts[1])
        except ValueError:
            continue
        if rate <= 0 or burst <= 0:
            continue
        out[template_id] = (rate, burst)
    return out


def template_key_for_request(
    *,
    template_id: str,
    claims_sub: str | None,
    remote_addr: str | None,
) -> str:
    """Stable cache key scoped to a template.

    Pattern: template:{template_id}:{sub_or_ip_key}.  Same shape as
    key_for_request but with the template prefix so two templates never
    share the same bucket.
    """
    inner = key_for_request(claims_sub, remote_addr)
    return f"template:{template_id}:{inner}"


class PerTemplateLimiter:
    """Token-bucket limiter with one bucket per (template, subject/IP).

    Quotas come from template_rates; requests for templates not in the
    map fall back to default_rate.  Process-local and thread-safe.
    """

    def __init__(
        self,
        *,
        template_rates: dict[str, tuple[int, int]],
        default_rate: tuple[int, int] = (60, 10),
    ) -> None:
        self.template_rates = dict(template_rates)
        self.default_rate = default_rate
        self._limiters: dict[str, TokenBucketLimiter] = {}
        self._lock = threading.Lock()

    def _limiter_for(self, template_id: str) -> TokenBucketLimiter:
        with self._lock:
            cached = self._limiters.get(template_id)
            if cached is not None:
                return cached
            rate, burst = self.template_rates.get(template_id, self.default_rate)
            limiter = TokenBucketLimiter(rate_per_minute=rate, burst=burst)
            self._limiters[template_id] = limiter
            return limiter

    def allow_for_template(
        self,
        template_id: str,
        claims_sub: str | None,
        remote_addr: str | None = None,
    ) -> RateLimitDecision:
        key = template_key_for_request(
            template_id=template_id, claims_sub=claims_sub, remote_addr=remote_addr,
        )
        return self._limiter_for(template_id).allow(key)

    def reset(self) -> None:
        with self._lock:
            for limiter in self._limiters.values():
                limiter.reset()
            self._limiters.clear()

    @classmethod
    def from_env(
        cls,
        env_var: str = "RATE_LIMIT_PER_TEMPLATE",
        default_rate: tuple[int, int] = (60, 10),
    ) -> PerTemplateLimiter:
        raw = os.getenv(env_var, "")
        rates = parse_template_rate_limit_env(raw)
        return cls(template_rates=rates, default_rate=default_rate)
