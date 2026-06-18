"""Rate-limit middleware for the platform API (spec §5.1).

Wraps a :class:`SlidingWindowLimiter` and runs as a FastAPI middleware.
Returns ``429 Too Many Requests`` + ``Retry-After`` header when the
bucket is full.

Exempt paths: ``/health``, ``/health/ready``, ``/docs``, ``/openapi.json``,
``/redoc``.  Keying is by ``X-User-Id`` header → client IP fallback.

Env:
  RATE_LIMIT_ENABLED=true|false (default false → no-op)
  RATE_LIMIT_LIMIT (default 60)
  RATE_LIMIT_WINDOW_SECONDS (default 60)
  REDIS_URL (optional; falls back to in-memory)
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ai_employee.agent_platform_api.rate_limit_redis import (
    SlidingWindowDecision,
    SlidingWindowLimiter,
    build_sliding_window_limiter,
    key_for_request,
)
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    pass

_EXEMPT_PREFIXES = (
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
)


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PREFIXES)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window middleware — installs at app construction time."""

    def __init__(self, app, *, limiter: SlidingWindowLimiter) -> None:
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)
        if os.getenv("RATE_LIMIT_ENABLED", "false").strip().lower() != "true":
            return await call_next(request)

        claims_sub = request.headers.get("X-User-Id") or request.headers.get("Authorization")
        remote_addr = request.client.host if request.client else None
        key = key_for_request(claims_sub, remote_addr)
        decision: SlidingWindowDecision = self.limiter.allow(key)
        if not decision.allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                headers={
                    "Retry-After": str(int(decision.retry_after_seconds)),
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response


def install_rate_limiter(app: FastAPI) -> None:
    """Attach :class:`RateLimitMiddleware` to ``app``."""
    limiter = build_sliding_window_limiter()
    app.add_middleware(RateLimitMiddleware, limiter=limiter)


__all__ = ["RateLimitMiddleware", "install_rate_limiter"]
