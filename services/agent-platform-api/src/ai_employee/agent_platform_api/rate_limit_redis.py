"""Re-export of the shared sliding-window limiter for backward compatibility.

The shared package lives at :mod:`packages.rate-limit.src.ai_employee.rate_limit`.
The original implementation moved there in R25-L.
"""

from ai_employee.rate_limit import (
    InMemoryBackend,
    RateLimitBackend,
    RedisBackend,
    SlidingWindowDecision,
    SlidingWindowLimiter,
    build_backend,
    build_sliding_window_limiter,
    iter_decisions,
    key_for_request,
)

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
