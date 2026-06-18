"""R21 agent-platform → mcp-gateway delegation (pluggable client).

Spec §9: tool registration + discovery + invocation move to a
standalone ``mcp-gateway``.  The agent-platform keeps its existing
``/api/v1/tools`` and ``/api/v1/mcp/tools`` endpoint contracts
(consumers are unaware) but delegates tool state to the gateway over
HTTP when ``MCP_GATEWAY_URL`` is set.  When unset, it falls back to the
in-memory store (backward compat / tests).

This file exercises both modes:

* **in-memory mode** (no env) — the platform uses
  :class:`InMemoryMcpGatewayClient`, which wraps the existing
  ``AgentPlatformStore.tools`` dict.  All legacy tool tests keep
  passing.
* **HTTP mode** (``MCP_GATEWAY_URL`` set) — the platform uses
  :class:`HttpMcpGatewayClient` against a real mcp-gateway
  ``TestClient`` mounted at a fake URL.  The platform returns the
  gateway's response shape to consumers.

A :class:`FakeMcpGatewayClient` is also used to assert the platform
calls the right client methods.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.clients import (
    FakeMcpGatewayClient,
    HttpMcpGatewayClient,
    InMemoryMcpGatewayClient,
    McpGatewayClient,
    build_mcp_client,
)
from ai_employee.mcp_gateway.app import create_app as create_mcp_app
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# In-memory mode (fallback): the legacy flows must keep working unchanged.
# --------------------------------------------------------------------------- #


def test_in_memory_mode_is_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_GATEWAY_URL", raising=False)
    client = build_mcp_client()
    assert isinstance(client, InMemoryMcpGatewayClient)


def test_in_memory_mode_register_and_list() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.post(
        "/api/v1/tools",
        json={
            "tool_name": "knowledge-api.chat.query",
            "service_name": "knowledge-api",
            "description": "Query knowledge base",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
    )
    assert resp.status_code == 201, resp.text
    listed = client.get("/api/v1/tools").json()
    names = {t["tool_name"] for t in listed["items"]}
    assert "knowledge-api.chat.query" in names


def test_in_memory_mode_mcp_tools_list_returns_registered() -> None:
    app = create_app()
    client = TestClient(app)
    client.post(
        "/api/v1/tools",
        json={
            "tool_name": "knowledge-api.chat.query",
            "service_name": "knowledge-api",
            "description": "Query knowledge base",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
    )
    body = client.get("/api/v1/mcp/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert "knowledge-api.chat.query" in names


# --------------------------------------------------------------------------- #
# HTTP mode: the platform delegates to a real mcp-gateway TestClient.
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_app(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Wire the platform to a real mcp-gateway via a fake URL.

    The HttpMcpGatewayClient is patched to talk to a TestClient of the
    mcp-gateway instead of opening a real socket, so the test is
    hermetic.
    """
    gateway_client = TestClient(create_mcp_app())

    def _fake_post(self, path, json):  # type: ignore[no-untyped-def]
        return gateway_client.post(path, json=json)

    def _fake_get(self, path, params=None):  # type: ignore[no-untyped-def]
        return gateway_client.get(path, params=params)

    monkeypatch.setattr(HttpMcpGatewayClient, "_post", _fake_post)
    monkeypatch.setattr(HttpMcpGatewayClient, "_get", _fake_get)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway.test")

    explicit = HttpMcpGatewayClient("http://mcp-gateway.test")
    return TestClient(create_app(mcp_client=explicit))


def test_http_mode_register_and_list_via_gateway(http_app: TestClient) -> None:
    resp = http_app.post(
        "/api/v1/tools",
        json={
            "tool_name": "knowledge-api.chat.query",
            "service_name": "knowledge-api",
            "description": "Query knowledge base",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
    )
    assert resp.status_code == 201, resp.text
    listed = http_app.get("/api/v1/tools").json()
    names = {t["tool_name"] for t in listed["items"]}
    assert "knowledge-api.chat.query" in names


def test_http_mode_mcp_tools_list_uses_gateway_shape(http_app: TestClient) -> None:
    http_app.post(
        "/api/v1/tools",
        json={
            "tool_name": "k.x",
            "service_name": "k",
            "description": "x",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
    )
    body = http_app.get("/api/v1/mcp/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert "k.x" in names
    assert "echo" in names  # gateway's built-in tool


def test_http_mode_register_unknown_risk_returns_400(http_app: TestClient) -> None:
    resp = http_app.post(
        "/api/v1/tools",
        json={
            "tool_name": "weird",
            "service_name": "x",
            "description": "x",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "impossible",
        },
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Fake client: assert the platform calls the protocol + merges MCP shape.
# --------------------------------------------------------------------------- #


def test_fake_client_records_register_call() -> None:
    fake = FakeMcpGatewayClient()
    app = create_app(mcp_client=fake)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/tools",
        json={
            "tool_name": "k.q",
            "service_name": "k",
            "description": "x",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
    )
    assert resp.status_code == 201
    # Fake records the call shape — the platform produced a registration
    # request against the gateway.
    assert any(call[0] == "register" for call in fake.calls)


def test_mcp_gateway_client_is_protocol() -> None:
    """McpGatewayClient is a runtime_checkable Protocol."""
    from typing import runtime_checkable

    assert runtime_checkable(McpGatewayClient)
    assert isinstance(InMemoryMcpGatewayClient(), McpGatewayClient)
    assert isinstance(HttpMcpGatewayClient("http://x"), McpGatewayClient)
    assert isinstance(FakeMcpGatewayClient(), McpGatewayClient)
