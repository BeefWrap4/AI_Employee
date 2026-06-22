"""api-gateway service (R32-A, spec §三 §5.1).

Single ingress-level API gateway for the AI Employee platform.  Owns
authentication, rate limiting, audit, routing, and trace_id + run_id
propagation so the six backend services stay independently deployable
but are reached through one front door.

Routing (path prefix → backend):

* ``/api/knowledge/*``  → knowledge-api:8010
* ``/api/rca/*``        → rca-agent:8020
* ``/api/platform/*``   → agent-platform-api:8030
* ``/api/tools/*``      → tool-registry:8040
* ``/api/approvals/*``  → approval-service:8040
* ``/api/mcp/*``        → mcp-gateway:8050

Cross-cutting concerns:

* **Authentication** — delegates to ``require_internal_or_jwt`` from the
  shared ``auth-policy`` package when ``API_GATEWAY_AUTH_REQUIRED=true``.
  Defaults to ``false`` (open) so dev/test traffic flows until an
  operator flips the switch; the auth decision is still audited.
* **Rate limiting** — ``install_rate_limiter`` from the shared
  ``rate-limit`` package (no-op unless ``RATE_LIMIT_ENABLED=true``).
* **trace_id** — mints a UUID when the caller sends no ``X-Trace-Id``
  and always propagates the resolved id to the backend + the response.
* **Audit** — every request (forwarded or rejected) appends a record
  to ``app.state.audit_log`` with ``trace_id`` / ``run_id`` / method /
  path / backend / status / timestamp.

The :class:`BackendProxy` Protocol lets tests inject a stub so no socket
is opened; production wires :class:`HttpBackendProxy` (httpx-based).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Protocol, runtime_checkable

import httpx
from ai_employee.rate_limit import install_rate_limiter
from fastapi import FastAPI, HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

SERVICE_VERSION = "0.1.0"

# path prefix → backend name.  Order matters only for readability; the
# prefixes are disjoint so the first match wins.
ROUTE_TABLE: list[tuple[str, str]] = [
    ("/api/knowledge", "knowledge-api"),
    ("/api/rca", "rca-agent"),
    ("/api/platform", "agent-platform-api"),
    ("/api/tools", "tool-registry"),
    ("/api/approvals", "approval-service"),
    ("/api/mcp", "mcp-gateway"),
]

# backend name → default upstream base URL.  Overridable via env
# ``API_GATEWAY_<NAME>_URL`` (e.g. ``API_GATEWAY_KNOWLEDGE_API_URL``).
DEFAULT_BACKEND_URLS: dict[str, str] = {
    "knowledge-api": "http://knowledge-api:8010",
    "rca-agent": "http://rca-agent:8020",
    "agent-platform-api": "http://agent-platform-api:8030",
    "tool-registry": "http://tool-registry:8040",
    "approval-service": "http://approval-service:8040",
    "mcp-gateway": "http://mcp-gateway:8050",
}

# Headers that are hop-by-hop / must not be blindly copied to the
# upstream request (RFC 7230 §6.1 + the framework's own framing).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# --------------------------------------------------------------------------- #
# Backend proxy Protocol + implementations
# --------------------------------------------------------------------------- #


@runtime_checkable
class BackendProxy(Protocol):
    """Forward a normalised request to a backend and return its response.

    ``path`` is the backend-relative path (the ``/api/<svc>`` prefix is
    already stripped).  ``headers`` includes the propagated ``X-Trace-Id``
    and ``X-Run-Id``.  The return dict carries the raw status code, the
    response headers, and the raw body bytes so the gateway can stream
    them back unchanged.
    """

    def forward(
        self,
        *,
        backend: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> dict[str, Any]: ...


def _backend_url(backend: str) -> str:
    """Resolve the upstream base URL for ``backend`` (env override first)."""
    env_key = f"API_GATEWAY_{backend.upper().replace('-', '_')}_URL"
    return os.getenv(env_key, DEFAULT_BACKEND_URLS[backend])


class HttpBackendProxy:
    """``httpx``-backed implementation of :class:`BackendProxy`.

    Production path.  Tests monkeypatch this class (or inject a stub)
    to avoid opening sockets.
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def forward(
        self,
        *,
        backend: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> dict[str, Any]:
        base = _backend_url(backend).rstrip("/")
        url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
        resp = httpx.request(  # pragma: no cover - real network path
            method,
            url,
            headers=headers,
            content=body,
            timeout=self.timeout,
        )
        # Drop hop-by-hop from the upstream response too.
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return {
            "status_code": resp.status_code,
            "headers": out_headers,
            "body": resp.content,
        }


# --------------------------------------------------------------------------- #
# Routing + trace helpers
# --------------------------------------------------------------------------- #


def _match_backend(path: str) -> tuple[str, str] | None:
    """Return ``(backend_name, backend_relative_path)`` or ``None``.

    The matched prefix is stripped so the backend sees its own natural
    path (e.g. ``/api/knowledge/v1/docs`` → ``knowledge-api`` +
    ``/v1/docs``).
    """
    for prefix, backend in ROUTE_TABLE:
        if path == prefix:
            return backend, "/"
        if path.startswith(prefix + "/"):
            return backend, path[len(prefix) :]
    return None


def _resolve_trace_id(request: Request) -> str:
    """Reuse the caller's ``X-Trace-Id`` or mint a new one."""
    incoming = request.headers.get("X-Trace-Id")
    if incoming and incoming.strip():
        return incoming.strip()
    return uuid.uuid4().hex


def _auth_required() -> bool:
    return os.getenv("API_GATEWAY_AUTH_REQUIRED", "false").strip().lower() == "true"


def _authenticate(request: Request) -> bool:
    """Return ``True`` when the request carries a valid credential.

    Delegates to the shared ``require_internal_or_jwt`` decision logic
    (JWT Bearer first, then ``X-Internal-Token``).  When auth is
    disabled (``API_GATEWAY_AUTH_REQUIRED=false``) the gate is open.
    """
    if not _auth_required():
        return True
    # Reuse the shared auth primitives so the gateway's auth matches the
    # rest of the platform (same JWT secret, same internal-token env).
    from ai_employee.auth_policy.fastapi_dep import (
        _claims_from_request,
        _internal_token_ok,
    )

    # JWT path: a valid Bearer token authenticates regardless of roles.
    try:
        if _claims_from_request(request) is not None:
            return True
    except HTTPException:
        # Invalid/expired JWT → fall through to the internal-token path
        # so a caller that sent a bad JWT but a good internal token still
        # gets a deterministic 401 (neither credential is valid on its
        # own merits once the JWT raised).  We return False below.
        return False
    return _internal_token_ok(request)


# --------------------------------------------------------------------------- #
# Audit middleware
# --------------------------------------------------------------------------- #


class AuditMiddleware(BaseHTTPMiddleware):
    """Mint/propagate trace_id, run_id and append an audit record per request.

    The middleware runs *before* the route handler so it can stamp the
    ``X-Trace-Id`` on the request state and the response.  The audit
    record is appended after the response is produced so it carries the
    final status code.
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next,  # type: ignore[no-untyped-def]
    ):  # type: ignore[no-untyped-def]
        # /health is not audited (liveness probe noise).
        if request.url.path == "/health":
            return await call_next(request)
        trace_id = _resolve_trace_id(request)
        run_id = request.headers.get("X-Run-Id")
        request.state.trace_id = trace_id
        request.state.run_id = run_id
        # The backend (if any) is resolved here so the audit record can
        # carry it even for rejected requests.
        match = _match_backend(request.url.path)
        backend = match[0] if match is not None else None

        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id

        audit_log: list[dict[str, Any]] = getattr(request.app.state, "audit_log", None)  # type: ignore[assignment]
        if audit_log is not None:
            audit_log.append(
                {
                    "trace_id": trace_id,
                    "run_id": run_id,
                    "method": request.method,
                    "path": request.url.path,
                    "backend": backend,
                    "status": response.status_code,
                    "ts": time.time(),
                }
            )
        return response


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app(
    *,
    backend_proxy: BackendProxy | None = None,
) -> FastAPI:
    """Build the api-gateway app.

    ``backend_proxy`` is optional in tests (inject a stub).  Production
    wires :class:`HttpBackendProxy`.
    """
    proxy = backend_proxy or HttpBackendProxy()

    app = FastAPI(title="AI Employee API Gateway", version=SERVICE_VERSION)
    app.state.audit_log: list[dict[str, Any]] = []
    app.state.backend_proxy = proxy

    # R25-L / R31-A: shared rate-limit middleware (no-op unless
    # RATE_LIMIT_ENABLED=true).  Mounted before the audit middleware so
    # 429 responses are still audited.
    install_rate_limiter(app)
    app.add_middleware(AuditMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "api-gateway",
            "status": "ok",
            "version": SERVICE_VERSION,
        }

    @app.api_route(
        "/api/{prefix}/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def gateway_route(prefix: str, rest: str, request: Request) -> Response:
        full_path = f"/api/{prefix}/{rest}"
        match = _match_backend(full_path)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "unknown_route",
                    "path": full_path,
                },
            )
        backend, backend_path = match

        # Authentication gate (401 on missing/invalid credentials).
        if not _authenticate(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "authentication_required"},
            )

        # Build the forwarded headers: copy the caller's headers minus
        # hop-by-hop, then stamp trace_id + run_id.
        forwarded_headers: dict[str, str] = {}
        for k, v in request.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            forwarded_headers[k] = v
        forwarded_headers["X-Trace-Id"] = request.state.trace_id
        if request.state.run_id:
            forwarded_headers["X-Run-Id"] = request.state.run_id

        body = await request.body()
        body_bytes = body if body else None

        try:
            result = proxy.forward(
                backend=backend,
                method=request.method,
                path=backend_path,
                headers=forwarded_headers,
                body=body_bytes,
            )
        except Exception as exc:
            logger.warning("api-gateway forward to %s failed: %s", backend, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "backend_unreachable",
                    "backend": backend,
                    "message": str(exc),
                },
            ) from exc

        # Stream the backend response back.  Drop hop-by-hop response
        # headers; the audit middleware has already stamped trace_id on
        # the *gateway* response.
        resp_headers = {
            k: v
            for k, v in (result.get("headers") or {}).items()
            if k.lower() not in _HOP_BY_HOP and k.lower() != "x-trace-id"
        }
        return Response(
            content=result.get("body") or b"",
            status_code=result.get("status_code", 200),
            headers=resp_headers,
            media_type=(result.get("headers") or {}).get("Content-Type"),
        )

    return app


app = create_app()


__all__ = [
    "DEFAULT_BACKEND_URLS",
    "ROUTE_TABLE",
    "AuditMiddleware",
    "BackendProxy",
    "HttpBackendProxy",
    "create_app",
]
