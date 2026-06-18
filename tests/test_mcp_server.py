"""MCP Python SDK integration tests (spec P3 §4 MCP Python SDK / FastMCP)."""
from __future__ import annotations

import json

import pytest

from ai_employee.agent_platform_api.mcp_server import (
    MCPToolRegistry,
    build_mcp_registry,
    to_mcp_tool,
)


# --------------------------------------------------------------------------- #
# to_mcp_tool — convert internal ToolRegistration to MCP Tool shape
# --------------------------------------------------------------------------- #


def test_to_mcp_tool_returns_mcp_tool() -> None:
    from ai_employee.agent_platform_api.schemas import ToolRegistration

    reg = ToolRegistration(
        tool_name="cmdb.lookup",
        service_name="cmdb",
        description="Look up a CMDB asset by id.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        risk_level="read_only",
    )
    tool = to_mcp_tool(reg)
    # MCP Tool is a TypedDict-like object with name/description/inputSchema.
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name")
    assert name == "cmdb.lookup"


def test_to_mcp_tool_risk_level_serialised() -> None:
    from ai_employee.agent_platform_api.schemas import ToolRegistration

    reg = ToolRegistration(
        tool_name="x", service_name="x", description="x",
        input_schema={"type": "object"}, output_schema={"type": "object"},
        risk_level="approval_required",
    )
    tool = to_mcp_tool(reg)
    dumped = tool if isinstance(tool, dict) else tool.model_dump()
    # Either embedded in annotations or top-level annotations field.
    text = json.dumps(dumped, default=str)
    assert "approval_required" in text


# --------------------------------------------------------------------------- #
# MCPToolRegistry — wraps our internal store in MCP shape
# --------------------------------------------------------------------------- #


def test_registry_list_tools_returns_mcp_shape() -> None:
    from ai_employee.agent_platform_api.schemas import ToolRegistration
    from ai_employee.agent_platform_api.runtime import AgentPlatformStore

    store = AgentPlatformStore()
    reg = ToolRegistration(
        tool_name="rca-agent.runs.create",
        service_name="rca-agent",
        description="Create an RCA run.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level="approval_required",
    )
    store.tools[reg.tool_name] = type("T", (), {
        "tool_name": reg.tool_name, "service_name": reg.service_name,
        "description": reg.description, "input_schema": reg.input_schema,
        "output_schema": reg.output_schema, "risk_level": reg.risk_level,
        "status": "active", "health_status": "healthy",
    })()

    registry = MCPToolRegistry(store=store)
    tools = registry.list_tools()
    assert any(t.get("name") == "rca-agent.runs.create" if isinstance(t, dict) else t.name == "rca-agent.runs.create" for t in tools)


def test_registry_to_list_tools_result() -> None:
    from ai_employee.agent_platform_api.runtime import AgentPlatformStore
    from mcp.types import ListToolsResult

    store = AgentPlatformStore()
    registry = MCPToolRegistry(store=store)
    result = registry.to_list_tools_result()
    assert isinstance(result, ListToolsResult)


def test_registry_filters_by_risk_level() -> None:
    from ai_employee.agent_platform_api.runtime import AgentPlatformStore

    store = AgentPlatformStore()
    for i, risk in enumerate(["read_only", "approval_required", "forbidden"]):
        store.tools[f"t{i}"] = type("T", (), {
            "tool_name": f"t{i}", "service_name": "x",
            "description": "x", "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": risk, "status": "active", "health_status": "healthy",
        })()

    registry = MCPToolRegistry(store=store)
    safe_tools = registry.list_tools(risk_levels={"read_only"})
    names = [t.get("name") if isinstance(t, dict) else t.name for t in safe_tools]
    assert "t0" in names
    assert "t1" not in names
    assert "t2" not in names


def test_registry_call_tool_dispatches_to_store() -> None:
    """``call_tool`` invokes the registered tool function and returns MCP result."""
    from ai_employee.agent_platform_api.runtime import AgentPlatformStore

    store = AgentPlatformStore()

    def fake_invoke(name, arguments):
        return {"result": f"echo {arguments.get('msg', '')}"}

    store.tools["echo"] = type("T", (), {
        "tool_name": "echo", "service_name": "x",
        "description": "echo back",
        "input_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "risk_level": "read_only", "status": "active", "health_status": "healthy",
        "invoke": fake_invoke,
    })()
    store.invoke = fake_invoke  # type: ignore[attr-defined]

    registry = MCPToolRegistry(store=store, invoke_fn=lambda name, args: fake_invoke(name, args))
    result = registry.call_tool("echo", {"msg": "hi"})
    # The MCP CallToolResult wraps a list of TextContent / structured.
    content = result.content if hasattr(result, "content") else result["content"]
    rendered = " ".join(
        c.text if hasattr(c, "text") else (c.get("text") if isinstance(c, dict) else str(c))
        for c in content
    )
    assert "echo hi" in rendered


# --------------------------------------------------------------------------- #
# build_mcp_registry — uses our AgentPlatformStore singleton
# --------------------------------------------------------------------------- #


def test_build_mcp_registry_uses_platform_store() -> None:
    from ai_employee.agent_platform_api.mcp_server import build_mcp_registry
    from ai_employee.agent_platform_api.runtime import AgentPlatformStore

    # Re-seed: the singleton store may already have tools from earlier tests.
    store = AgentPlatformStore()
    store.tools.clear()
    registry = build_mcp_registry()
    assert isinstance(registry, MCPToolRegistry)


# --------------------------------------------------------------------------- #
# FastMCP server wiring (smoke test — full protocol is exercised by SDK)
# --------------------------------------------------------------------------- #


def test_mcp_server_module_exposes_app_factory() -> None:
    """The module exports a ``create_mcp_server`` function that returns a
    :class:`mcp.server.Server` pre-loaded with our tools."""
    from ai_employee.agent_platform_api.mcp_server import create_mcp_server
    from mcp.server import Server

    server = create_mcp_server()
    assert isinstance(server, Server)
