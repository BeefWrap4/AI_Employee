"""MCP Python SDK integration (spec P3 §4 MCP Python SDK / FastMCP).

Replaces the self-built ``to_mcp_list()`` JSON contract with the
official MCP Python SDK (:mod:`mcp.server` / :mod:`mcp.types`).  The
:class:`MCPToolRegistry` adapts our internal tool store to MCP's
``Tool`` / ``ListToolsResult`` / ``CallToolResult`` shapes so the
agent-platform exposes a spec-conformant MCP endpoint.

Backwards compatible: the platform app's existing ``/api/v1/mcp/tools``
endpoint now delegates to :meth:`MCPToolRegistry.to_list_tools_result`,
so external clients see the same shape but it's now produced by the
SDK (not a hand-rolled dict).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from mcp.server import Server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from ai_employee.agent_platform_api.schemas import ToolRegistration

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Type adapters
# --------------------------------------------------------------------------- #


def to_mcp_tool(reg: ToolRegistration) -> Tool:
    """Convert an internal :class:`ToolRegistration` to the SDK's :class:`Tool`.

    The risk level is carried as MCP annotations so policy-aware clients
    can render it without an out-of-band lookup.
    """
    annotations = {
        "risk_level": reg.risk_level,
        "service_name": reg.service_name,
    }
    return Tool(
        name=reg.tool_name,
        description=reg.description,
        inputSchema=reg.input_schema,
        annotations=annotations,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Registry — wraps an AgentPlatformStore (or any compatible store)
# --------------------------------------------------------------------------- #


InvokeFn = Callable[[str, dict[str, Any]], dict[str, Any]]


class MCPToolRegistry:
    """Adapts our internal tool store to the MCP protocol shapes.

    ``invoke_fn`` is the dispatch callable (defaults to
    ``store.invoke``) so tests can swap the dispatch without touching
    the store.
    """

    def __init__(
        self,
        *,
        store: Any,
        invoke_fn: InvokeFn | None = None,
    ) -> None:
        self._store = store
        self._invoke_fn: InvokeFn = invoke_fn or _default_invoke(store)

    def list_tools(
        self,
        *,
        risk_levels: set[str] | None = None,
    ) -> list[Tool]:
        """List tools, optionally filtered by allowed risk levels."""
        tools: list[Tool] = []
        for tool in self._store.tools.values():
            if risk_levels is not None and tool.risk_level not in risk_levels:
                continue
            reg = ToolRegistration(
                tool_name=tool.tool_name,
                service_name=tool.service_name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                risk_level=tool.risk_level,
            )
            tools.append(to_mcp_tool(reg))
        return tools

    def to_list_tools_result(
        self,
        *,
        risk_levels: set[str] | None = None,
    ) -> ListToolsResult:
        """Build an MCP :class:`ListToolsResult` for ``list_tools``."""
        return ListToolsResult(tools=self.list_tools(risk_levels=risk_levels))

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        """Dispatch an MCP ``tools/call`` to the underlying store."""
        try:
            result = self._invoke_fn(name, arguments)
        except Exception as exc:  # noqa: BLE001
            return CallToolResult(
                content=[TextContent(type="text", text=f"error: {exc}")],
                isError=True,
            )
        return CallToolResult(
            content=[TextContent(type="text", text=_to_text(result))],
        )


def _default_invoke(store: Any) -> InvokeFn:
    """Build a default invoke fn that delegates to ``store.invoke``."""
    def _invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = store.tools.get(name)
        if tool is None:
            raise KeyError(f"tool not found: {name}")
        invoke = getattr(tool, "invoke", None)
        if invoke is None:
            # Fallback: ask the store directly (legacy path).
            if hasattr(store, "invoke"):
                return store.invoke(name, arguments)
            raise RuntimeError(f"tool {name!r} has no invoke handler")
        return invoke(arguments)
    return _invoke


def _to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        import json
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


# --------------------------------------------------------------------------- #
# Factory + FastMCP-style server
# --------------------------------------------------------------------------- #


def build_mcp_registry() -> MCPToolRegistry:
    """Build a registry backed by the singleton platform tool store."""
    from ai_employee.agent_platform_api.runtime import AgentPlatformStore

    return MCPToolRegistry(store=AgentPlatformStore())


def create_mcp_server() -> Server:
    """Create an :class:`mcp.server.Server` with our tools wired in.

    The server exposes ``list_tools`` + ``call_tool`` over the MCP
    protocol.  It's a stdio-transport server by default; the
    platform app can wrap it in a FastAPI endpoint if HTTP is needed.
    """
    from mcp.server import Server
    import mcp.types as types

    server: Server = Server("ai-employee-platform")
    registry = build_mcp_registry()

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return registry.list_tools()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.ContentBlock]:
        result = registry.call_tool(name, arguments)
        return result.content

    return server


__all__ = [
    "MCPToolRegistry",
    "build_mcp_registry",
    "create_mcp_server",
    "to_mcp_tool",
]