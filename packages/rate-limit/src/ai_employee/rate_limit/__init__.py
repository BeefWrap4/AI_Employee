"""Shared rate-limit package (R25-L, spec §5.1 API gateway).

Sliding-window limiter with two backends (in-memory + Redis) + FastAPI
middleware.  Reused across all 6 services via
``install_rate_limiter(app, ...)``.

Env:
  RATE_LIMIT_ENABLED (default false → no-op)
  RATE_LIMIT_LIMIT (default 60)
  RATE_LIMIT_WINDOW_SECONDS (default 60)
  REDIS_URL (optional; falls back to in-memory)
"""

from ai_employee.rate_limit.limiter import (
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
from ai_employee.rate_limit.middleware import (
    KeyFunc,
    RateLimitMiddleware,
    install_rate_limiter,
    key_by_endpoint,
    key_by_tenant,
    key_by_tool,
    key_by_user,
)

__all__ = [
    "InMemoryBackend",
    "KeyFunc",
    "RateLimitBackend",
    "RateLimitMiddleware",
    "RedisBackend",
    "SlidingWindowDecision",
    "SlidingWindowLimiter",
    "build_backend",
    "build_sliding_window_limiter",
    "install_rate_limiter",
    "iter_decisions",
    "key_by_endpoint",
    "key_by_tenant",
    "key_by_tool",
    "key_by_user",
    "key_for_request",
]
