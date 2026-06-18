"""mcp-gateway: unified MCP-compatible tool gateway (spec §9).

Owns tool registration, discovery (MCP ``tools/list``), routing, and
invocation with resilience (timeout / retry / circuit breaker).  The
agent-platform delegates tool calls here over HTTP when
``MCP_GATEWAY_URL`` is set (see ``agent_platform_api.clients``).

Reuses :class:`ai_employee.common_schemas.tool_registry.ToolRegistry`
for the in-process registry + MCP shape, and
:mod:`ai_employee.agent_platform_api.tool_resilience` for the circuit
breaker so the gateway does not re-implement the spec §5.3 guarantees.
"""

from __future__ import annotations

from ai_employee.mcp_gateway.app import app, create_app

__all__ = ["app", "create_app"]
