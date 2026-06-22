"""FastAPI middleware for the shared rate-limit package (R25-L).

Env:
  RATE_LIMIT_ENABLED (default false → no-op)
  RATE_LIMIT_LIMIT (default 60)
  RATE_LIMIT_WINDOW_SECONDS (default 60)
  RATE_LIMIT_KEY_FUNC (default user; one of: user|tenant|endpoint|tool)

The ``key_func`` parameter (R31-A) lets a service choose the throttling
dimension — per user (default), per tenant, per endpoint, or per tool —
without copying the middleware.  Four built-in factories cover the
common cases; a service may also pass its own ``Callable[[Request], str]``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable

from ai_employee.rate_limit.limiter import (
    SlidingWindowDecision,
    SlidingWindowLimiter,
    build_sliding_window_limiter,
    key_for_request,
)
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# A key_func maps an incoming request to a single bucket key.  Returning
# ``None`` is not allowed — the factories always fall back to a stable
# sentinel so an empty header cannot collapse every anonymous request
# into the same in-process bucket as a real caller.
KeyFunc = Callable[[Request], str]

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
    def __init__(
        self, app, *, limiter: SlidingWindowLimiter, key_func: KeyFunc | None = None
    ) -> None:
        super().__init__(app)
        self.limiter = limiter
        self.key_func = key_func or key_by_user

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)
        if os.getenv("RATE_LIMIT_ENABLED", "false").strip().lower() != "true":
            return await call_next(request)

        key = self.key_func(request)
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


# ``/tools/{name}/invoke`` is the agent-platform tool-invocation path.
# We extract ``{name}`` cheaply with a regex rather than mounting a route
# matcher, so the factory stays dependency-free and works for any service
# that reuses this path shape.
_TOOL_PATH = re.compile(r"/tools/(?P<name>[^/]+)/invoke")


def key_by_user(request: Request) -> str:
    """Default dimension: ``X-User-Id`` → ``Authorization`` → client IP.

    Backward-compatible with the pre-R31 behaviour.
    """
    claims_sub = request.headers.get("X-User-Id") or request.headers.get("Authorization")
    remote_addr = request.client.host if request.client else None
    return key_for_request(claims_sub, remote_addr)


def key_by_tenant(request: Request) -> str:
    """Multi-tenant dimension: bucket by ``X-Tenant-ID`` header.

    Falls back to the caller IP (then ``ip:unknown``) when the header is
    absent, so a missing tenant header cannot be used to bypass limits.
    """
    tenant = request.headers.get("X-Tenant-ID")
    if tenant:
        return f"tenant:{tenant}"
    remote_addr = request.client.host if request.client else None
    return key_for_request(None, remote_addr)


def key_by_endpoint(request: Request) -> str:
    """Per-endpoint dimension: bucket by request path + client IP.

    Two paths hitting the same IP do not share a quota.
    """
    remote_addr = request.client.host if request.client else "unknown"
    return f"ep:{request.url.path}:{remote_addr}"


def key_by_tool(request: Request) -> str:
    """Per-tool dimension: bucket by tool_name + user on
    ``/tools/{name}/invoke`` paths.

    Non-tool paths fall back to the user dimension so the limiter still
    applies meaningfully to the rest of the API surface.
    """
    m = _TOOL_PATH.search(request.url.path)
    if m:
        tool_name = m.group("name")
        user = request.headers.get("X-User-Id") or "anon"
        return f"tool:{tool_name}:{user}"
    return key_by_user(request)


# Maps the ``RATE_LIMIT_KEY_FUNC`` env value to a factory.  Services that
# need a bespoke dimension can pass a callable directly to
# :func:`install_rate_limiter` instead of using this table.
_KEY_FUNC_REGISTRY: dict[str, KeyFunc] = {
    "user": key_by_user,
    "tenant": key_by_tenant,
    "endpoint": key_by_endpoint,
    "tool": key_by_tool,
}


def _resolve_key_func(key_func: KeyFunc | str | None) -> KeyFunc | None:
    """Resolve a key_func that may be a callable, a registry name, or None."""
    if key_func is None:
        return None
    if callable(key_func):
        return key_func
    # ``str`` — look up by env-style name.
    try:
        return _KEY_FUNC_REGISTRY[str(key_func).strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"unknown RATE_LIMIT_KEY_FUNC {key_func!r}; "
            f"expected one of {sorted(_KEY_FUNC_REGISTRY)}"
        ) from exc


def install_rate_limiter(
    app: FastAPI,
    *,
    key_func: KeyFunc | str | None = None,
    requests: int | None = None,
    window: int | None = None,
) -> None:
    """Attach :class:`RateLimitMiddleware` to ``app`` (one-line setup).

    Parameters
    ----------
    key_func:
        Throttling dimension.  Either a ``Callable[[Request], str]``, one of
        the registry names (``"user"`` / ``"tenant"`` / ``"endpoint"`` /
        ``"tool"``), or ``None`` to consult the ``RATE_LIMIT_KEY_FUNC`` env
        var (default ``user`` — backward compatible).
    requests:
        Optional override for the per-window limit (defaults to
        ``RATE_LIMIT_LIMIT`` env / 60).
    window:
        Optional override for the window in seconds (defaults to
        ``RATE_LIMIT_WINDOW_SECONDS`` env / 60).
    """
    limiter = build_sliding_window_limiter(
        limit=requests,
        window_seconds=window,
    )
    resolved = _resolve_key_func(key_func)
    if resolved is None:
        env_name = os.getenv("RATE_LIMIT_KEY_FUNC", "user").strip().lower() or "user"
        resolved = _resolve_key_func(env_name)
    app.add_middleware(RateLimitMiddleware, limiter=limiter, key_func=resolved)


__all__ = [
    "KeyFunc",
    "RateLimitMiddleware",
    "install_rate_limiter",
    "key_by_endpoint",
    "key_by_tenant",
    "key_by_tool",
    "key_by_user",
]
