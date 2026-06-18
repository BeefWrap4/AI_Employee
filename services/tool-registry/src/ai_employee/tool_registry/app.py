"""tool-registry service: register, list, and invoke tools over HTTP.

Exposes the :class:`ai_employee.common_schemas.tool_registry.ToolRegistry`
over a small FastAPI surface with JWT-based auth:

* ``POST /api/v1/tools`` — register/update a tool (``tool:register``)
* ``GET /api/v1/tools`` — list tools in MCP ``tools/list`` shape (read-only)
* ``GET /api/v1/tools/{name}`` — fetch a single tool spec
* ``POST /api/v1/tools/{name}/invoke`` — invoke a registered tool
  (``tool:invoke`` for read_only, ``agent:approve`` for
  approval_required, admin for high_risk)

Handlers are bound at registration time (in-memory) and also recorded
declaratively in SQLite so the registry survives restarts.  Built-in
demo tools (e.g. ``echo``) are registered on startup for smoke testing.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from ai_employee.auth_policy import (
    PERM_AGENT_APPROVE,
    PERM_TOOL_INVOKE,
    PERM_TOOL_REGISTER,
    TokenClaims,
    require_internal_or_jwt,
)
from ai_employee.common_schemas.tool_registry import (
    ToolInvocationError,
    ToolNotFound,
    ToolRegistry,
    ToolSpec,
)
from ai_employee.tool_registry.store import ToolRegistryStore

SERVICE_VERSION = "0.1.0"


class ToolRegistrationRequest(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "read_only"
    service_name: str | None = None
    version: str = "v1"


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def _risk_levels() -> set[str]:
    return {"read_only", "approval_required", "high_risk"}


def _to_spec(payload: ToolRegistrationRequest, handler=None) -> ToolSpec:
    return ToolSpec(
        name=payload.name,
        description=payload.description,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        risk_level=payload.risk_level,
        service_name=payload.service_name,
        version=payload.version,
        handler=handler,
    )


def _register_builtin_tools(registry: ToolRegistry) -> None:
    """Seed a couple of read-only demo tools so the service is usable on boot."""

    def _echo(text: str = "") -> dict[str, Any]:
        return {"echo": text}

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
            service_name="tool-registry",
            handler=_echo,
        )
    )


def create_app(
    store: ToolRegistryStore | None = None,
    *,
    registry: ToolRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Tool Registry", version=SERVICE_VERSION)
    registry_state = registry or ToolRegistry()
    store_state = store or ToolRegistryStore()
    _register_builtin_tools(registry_state)
    # Persist the built-in tools so GET reflects them even before any
    # explicit registration call.
    for spec in registry_state.list():
        store_state.upsert({
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
            "risk_level": spec.risk_level,
            "service_name": spec.service_name,
            "version": spec.version,
        })

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"service": "tool-registry", "status": "ok", "version": SERVICE_VERSION}

    @app.get("/api/v1/tools")
    def list_tools(service_name: str | None = None) -> dict[str, Any]:
        """List tools in MCP ``tools/list`` shape. Read-only, no auth."""
        rows = store_state.list(service_name=service_name)
        out = ToolRegistry()
        for row in rows:
            out.register(
                ToolSpec(
                    name=row["name"],
                    description=row["description"],
                    input_schema=row["input_schema"],
                    output_schema=row["output_schema"],
                    risk_level=row["risk_level"],
                    service_name=row.get("service_name"),
                    version=row.get("version", "v1"),
                ),
                replace=True,
            )
        return out.to_mcp_list()

    @app.get("/api/v1/tools/{name}")
    def get_tool(name: str) -> dict[str, Any]:
        row = store_state.get(name)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "tool_not_found", "tool_name": name},
            )
        return row

    @app.post(
        "/api/v1/tools",
        status_code=status.HTTP_201_CREATED,
    )
    def register_tool(
        payload: ToolRegistrationRequest,
        claims: TokenClaims | None = Depends(
            require_internal_or_jwt([PERM_TOOL_REGISTER])
        ),
    ) -> dict[str, Any]:
        if payload.risk_level not in _risk_levels():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "invalid_risk_level",
                    "risk_level": payload.risk_level,
                    "allowed": sorted(_risk_levels()),
                },
            )
        spec = _to_spec(payload)
        registry_state.register(spec, replace=True)
        store_state.upsert({
            "name": payload.name,
            "description": payload.description,
            "input_schema": payload.input_schema,
            "output_schema": payload.output_schema,
            "risk_level": payload.risk_level,
            "service_name": payload.service_name,
            "version": payload.version,
        })
        return {
            "name": payload.name,
            "registered": True,
            "registered_by": claims.sub if claims else "internal",
        }

    @app.post("/api/v1/tools/{name}/invoke")
    def invoke_tool(
        name: str,
        payload: ToolInvokeRequest,
        claims: TokenClaims | None = Depends(
            require_internal_or_jwt([PERM_TOOL_INVOKE])
        ),
    ) -> dict[str, Any]:
        row = store_state.get(name)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "tool_not_found", "tool_name": name},
            )
        # Enforce risk-level-specific permission.
        risk_level = row["risk_level"]
        required = (
            PERM_AGENT_APPROVE if risk_level == "approval_required"
            else PERM_TOOL_INVOKE if risk_level == "read_only"
            else "*"
        )
        from ai_employee.auth_policy import can_any
        if claims is not None:
            decision = can_any(claims.roles, claims.scopes, [required])
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "forbidden",
                        "required_permission": required,
                        "missing": decision.missing,
                    },
                )
        # Handlers live in the in-memory registry; built-in tools always
        # have one.  Tools registered over HTTP without a handler cannot
        # be invoked.
        try:
            spec = registry_state.get(name)
        except ToolNotFound:
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
        try:
            result = registry_state.invoke(name, payload.arguments)
        except ToolInvocationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "tool_invocation_error", "message": str(exc)},
            ) from exc
        return {
            "tool_name": name,
            "result": result,
            "invoked_by": claims.sub if claims else "internal",
        }

    return app


app = create_app()
