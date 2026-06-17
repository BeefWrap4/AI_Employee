"""MCP-compatible tools/list endpoint test."""
from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.agent_platform_api.app import create_app


def test_mcp_tools_endpoint_returns_registered_tools() -> None:
    client = TestClient(create_app())
    # Register one tool via the existing /api/v1/tools endpoint.
    register = client.post(
        "/api/v1/tools",
        json={
            "tool_name": "knowledge-api.chat.query",
            "service_name": "knowledge-api",
            "description": "Query knowledge base",
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            },
            "risk_level": "read_only",
        },
    )
    assert register.status_code == 201, register.text

    resp = client.get("/api/v1/mcp/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "tools" in body
    assert any(t["name"] == "knowledge-api.chat.query" for t in body["tools"])
    tool = next(t for t in body["tools"] if t["name"] == "knowledge-api.chat.query")
    assert tool["inputSchema"]["properties"]["question"]["type"] == "string"
    assert tool["metadata"]["risk_level"] == "read_only"
    assert tool["metadata"]["service_name"] == "knowledge-api"


def test_mcp_tools_endpoint_empty_returns_empty_list() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/mcp/tools")
    assert resp.status_code == 200
    assert resp.json() == {"tools": []}
