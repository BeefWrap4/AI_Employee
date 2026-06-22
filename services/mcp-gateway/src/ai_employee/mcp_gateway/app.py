"""mcp-gateway FastAPI app (spec §9).

Unified MCP-compatible front door for tool registration, discovery
(``tools/list``), routing, and invocation with a circuit breaker.

Endpoints:

* ``GET  /health``
* ``POST /api/v1/tools``                 — register a tool
* ``GET  /api/v1/tools``                 — list (MCP ``tools/list`` shape)
* ``GET  /api/v1/tools/{name}``          — fetch one
* ``POST /api/v1/tools/{name}/invoke``   — invoke a registered tool
* ``GET  /api/v1/tools/{name}/health``    — circuit-breaker state

Built-in ``echo`` tool is seeded on startup.  Tools registered over
HTTP without a ``handler_kind`` are listable but not invokable (mirror
of the tool-registry contract).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ai_employee.common_schemas.tool_registry import (
    ToolInvocationError,
    ToolNotFound,
    ToolRegistry,
    ToolSpec,
)
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

SERVICE_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Circuit breaker (spec §5.3) — self-contained so the gateway deploys
# without importing a sibling service.
# --------------------------------------------------------------------------- #


class CircuitOpenError(Exception):
    """Raised when a call is attempted against an open circuit."""


class CircuitBreaker:
    """Per-tool circuit breaker (closed → open → half-open)."""

    def __init__(self, *, failure_threshold: int = 5, recovery_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures: dict[str, int] = {}
        self._state: dict[str, str] = {}
        self._opened_at: dict[str, float] = {}

    def state(self, tool_name: str) -> str:
        current = self._state.get(tool_name, "closed")
        if current == "open":
            opened = self._opened_at.get(tool_name, 0.0)
            if time.time() - opened >= self.recovery_seconds:
                self._state[tool_name] = "half_open"
                return "half_open"
        return current

    def call(self, tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.state(tool_name) == "open":
            raise CircuitOpenError(f"circuit open for tool {tool_name!r}")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._on_failure(tool_name)
            raise
        self._on_success(tool_name)
        return result

    def _on_success(self, tool_name: str) -> None:
        self._failures[tool_name] = 0
        self._state[tool_name] = "closed"
        self._opened_at.pop(tool_name, None)

    def _on_failure(self, tool_name: str) -> None:
        self._failures[tool_name] = self._failures.get(tool_name, 0) + 1
        if (
            self._failures[tool_name] >= self.failure_threshold
            or self._state.get(tool_name) == "half_open"
        ):
            self._state[tool_name] = "open"
            self._opened_at[tool_name] = time.time()


# --------------------------------------------------------------------------- #
# Built-in handlers + handler registry
# --------------------------------------------------------------------------- #


def _echo_handler(text: str = "") -> dict[str, Any]:
    return {"echo": text}


def _noop_handler(**arguments: Any) -> dict[str, Any]:
    return {"ok": True, "arguments": arguments}


# ``handler_kind`` → callable.  Lets HTTP registrations bind to a
# known demo handler without serialising a Python callable over HTTP.
_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "echo": _echo_handler,
    "noop": _noop_handler,
}


def _risk_levels() -> set[str]:
    return {
        "readonly",
        "suggest",
        "approval_required",
        "forbidden",
        "read_only",
        "high_risk",
    }


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class ToolRegistrationRequest(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "read_only"
    service_name: str | None = None
    version: str = "v1"
    timeout_ms: int = 5000
    health_check_url: str | None = None
    # Optional demo-handler binding (echo | noop).  Tools registered
    # without one are listable but not invokable over HTTP.
    handler_kind: str | None = None


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def _register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="echo",
            description="Echo back the input text (demo read-only tool).",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
            },
            risk_level="read_only",
            service_name="mcp-gateway",
            handler=_echo_handler,
        )
    )


def create_app(
    *,
    registry: ToolRegistry | None = None,
    circuit_breaker: CircuitBreaker | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee MCP Gateway", version=SERVICE_VERSION)
    # R25-L: shared rate-limit middleware (no-op unless RATE_LIMIT_ENABLED=true).
    from ai_employee.rate_limit import install_rate_limiter

    install_rate_limiter(app)
    state = registry or ToolRegistry()
    breaker = circuit_breaker or CircuitBreaker()
    _register_builtin_tools(state)

    def _get_spec_or_404(name: str) -> ToolSpec:
        try:
            return state.get(name)
        except ToolNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "tool_not_found", "tool_name": name},
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"service": "mcp-gateway", "status": "ok", "version": SERVICE_VERSION}

    @app.get("/api/v1/tools")
    def list_tools(service_name: str | None = None) -> dict[str, Any]:
        """List tools in MCP ``tools/list`` shape (read-only)."""
        tools = state.list_by_service(service_name) if service_name else state.list()
        out = ToolRegistry()
        for spec in tools:
            out.register(
                ToolSpec(
                    name=spec.name,
                    description=spec.description,
                    input_schema=spec.input_schema,
                    output_schema=spec.output_schema,
                    risk_level=spec.risk_level,
                    service_name=spec.service_name,
                    version=spec.version,
                    timeout_ms=spec.timeout_ms,
                    retry_policy=spec.retry_policy,
                    health_check_url=spec.health_check_url,
                ),
                replace=True,
            )
        return out.to_mcp_list()

    @app.get("/api/v1/tools/{name}")
    def get_tool(name: str) -> dict[str, Any]:
        spec = _get_spec_or_404(name)
        return spec.to_mcp_tool()

    @app.post(
        "/api/v1/tools",
        status_code=status.HTTP_201_CREATED,
    )
    def register_tool(payload: ToolRegistrationRequest) -> dict[str, Any]:
        if payload.risk_level not in _risk_levels():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "invalid_risk_level",
                    "risk_level": payload.risk_level,
                    "allowed": sorted(_risk_levels()),
                },
            )
        if payload.name in {t.name for t in state.list()}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "tool_already_registered",
                    "tool_name": payload.name,
                },
            )
        handler = _HANDLERS.get(payload.handler_kind) if payload.handler_kind else None
        spec = ToolSpec(
            name=payload.name,
            description=payload.description,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            risk_level=payload.risk_level,
            service_name=payload.service_name,
            version=payload.version,
            timeout_ms=payload.timeout_ms,
            retry_policy={"max_retries": 0},
            health_check_url=payload.health_check_url,
            handler=handler,
        )
        state.register(spec, replace=True)
        return {"name": payload.name, "registered": True}

    @app.post("/api/v1/tools/{name}/invoke")
    def invoke_tool(name: str, payload: ToolInvokeRequest) -> dict[str, Any]:
        spec = _get_spec_or_404(name)
        # Forbidden tools can never be invoked through the gateway.
        if spec.risk_level == "forbidden":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "tool_forbidden",
                    "tool_name": name,
                    "message": "tool is marked forbidden; invocation is disabled",
                },
            )
        if spec.handler is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "tool_not_invokable",
                    "message": (
                        "tool has no in-memory handler; it was registered "
                        "declaratively and cannot be invoked over HTTP"
                    ),
                },
            )
        started = time.monotonic()
        try:
            result = breaker.call(name, state.invoke, name, payload.arguments)
        except CircuitOpenError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "circuit_open",
                    "tool_name": name,
                    "message": "tool circuit breaker is open; retry later",
                },
            )
        except ToolInvocationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "tool_invocation_error", "message": str(exc)},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error_code": "tool_failed", "message": str(exc)},
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "tool_name": name,
            "result": result,
            "latency_ms": latency_ms,
        }

    @app.get("/api/v1/tools/{name}/health")
    def tool_health(name: str) -> dict[str, Any]:
        _get_spec_or_404(name)
        return {
            "tool_name": name,
            "healthy": breaker.state(name) == "closed",
            "circuit_state": breaker.state(name),
        }

    return app


app = create_app()
