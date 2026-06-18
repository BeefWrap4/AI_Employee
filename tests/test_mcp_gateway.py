"""R21 mcp-gateway service: unified tool register/discover/route/invoke.

Spec §9 lists ``mcp-gateway`` as a standalone deployable unit — the
single MCP-compatible front door for tool registration, discovery,
routing, and invocation (with resilience: timeout / retry / circuit
breaker).  These tests exercise the service directly over HTTP.

The gateway owns an in-process tool registry (reusing
``ai_employee.common_schemas.tool_registry.ToolRegistry``) and exposes:

* ``POST /api/v1/tools``                 — register a tool
* ``GET  /api/v1/tools``                 — list (MCP ``tools/list`` shape)
* ``GET  /api/v1/tools/{name}``          — fetch one
* ``POST /api/v1/tools/{name}/invoke``   — invoke a registered tool
* ``GET  /api/v1/tools/{name}/health``    — circuit-breaker state

Built-in ``echo`` tool is seeded on startup for smoke testing.
"""

from __future__ import annotations

import pytest
from ai_employee.mcp_gateway.app import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_health() -> None:
    resp = TestClient(create_app()).get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "mcp-gateway"


def test_list_tools_returns_mcp_shape_with_builtin_echo(client: TestClient) -> None:
    resp = client.get("/api/v1/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert "tools" in body
    names = {t["name"] for t in body["tools"]}
    assert "echo" in names
    # MCP tools/list shape: name + inputSchema.
    echo = next(t for t in body["tools"] if t["name"] == "echo")
    assert echo["inputSchema"]["type"] == "object"


def test_register_tool_then_list(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools",
        json={
            "name": "knowledge-api.chat.query",
            "description": "Query knowledge base",
            "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
            "risk_level": "read_only",
            "service_name": "knowledge-api",
        },
    )
    assert resp.status_code == 201, resp.text
    listed = client.get("/api/v1/tools").json()
    names = {t["name"] for t in listed["tools"]}
    assert "knowledge-api.chat.query" in names


def test_register_duplicate_tool_rejected(client: TestClient) -> None:
    payload = {
        "name": "dup",
        "description": "dup",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "risk_level": "read_only",
    }
    assert client.post("/api/v1/tools", json=payload).status_code == 201
    again = client.post("/api/v1/tools", json=payload)
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "tool_already_registered"


def test_invoke_builtin_echo(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/echo/invoke",
        json={"arguments": {"text": "hello"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool_name"] == "echo"
    assert body["result"] == {"echo": "hello"}
    assert "latency_ms" in body


def test_invoke_unknown_tool_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/missing/invoke",
        json={"arguments": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "tool_not_found"


def test_invoke_forbidden_tool_returns_403(client: TestClient) -> None:
    client.post(
        "/api/v1/tools",
        json={
            "name": "danger",
            "description": "forbidden",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "forbidden",
            "handler_kind": "echo",
        },
    )
    resp = client.post("/api/v1/tools/danger/invoke", json={"arguments": {}})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "tool_forbidden"


def test_get_single_tool(client: TestClient) -> None:
    resp = client.get("/api/v1/tools/echo")
    assert resp.status_code == 200
    assert resp.json()["name"] == "echo"


def test_get_unknown_tool_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/tools/missing").status_code == 404


def test_tool_health_returns_circuit_state(client: TestClient) -> None:
    resp = client.get("/api/v1/tools/echo/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_name"] == "echo"
    assert body["circuit_state"] == "closed"
