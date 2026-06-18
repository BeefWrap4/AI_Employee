"""R21.5: Regression test for the R21 HttpMcpGatewayClient adapter bug.

B1: When a tool is registered to mcp-gateway without ``metadata.service_name``,
``HttpMcpGatewayClient.list_tools`` returned ``service_name=None``, but
``ToolResponse.service_name: str`` rejects None → platform ``GET /api/v1/tools``
returns 500. This test pins the fix (service_name accepts None / defaults to
"unknown").
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_mcp_gateway(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Spin up agent-platform pointed at an in-process mcp-gateway that has a
    tool registered WITHOUT ``metadata.service_name`` (the bug-trigger shape)."""
    from ai_employee.mcp_gateway import app as mcp_app

    gateway_client = TestClient(mcp_app)
    monkeypatch.setattr(
        "ai_employee.agent_platform_api.clients.httpx.get",
        lambda *a, **kw: gateway_client.get(kw.get("url") or a[0], params=kw.get("params")),
    )
    monkeypatch.setattr(
        "ai_employee.agent_platform_api.clients.httpx.post",
        lambda *a, **kw: gateway_client.post(kw.get("url") or a[0], json=kw.get("json")),
    )
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway-stub:8050")
    return TestClient(create_app())


def test_list_tools_does_not_500_when_gateway_tool_missing_service_name(
    client_with_mcp_gateway: TestClient,
) -> None:
    """A tool registered to the gateway without metadata.service_name must not
    crash the platform's ``GET /api/v1/tools`` (the B1 regression)."""
    from ai_employee.mcp_gateway import app as mcp_app
    from fastapi.testclient import TestClient as TC

    gateway = TC(mcp_app)
    r = gateway.post(
        "/api/v1/tools",
        json={
            "name": "no_meta",
            "description": "registered without service_name",
            "risk_level": "read_only",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            # service_name intentionally absent
        },
    )
    assert r.status_code == 201, r.text

    # Platform delegation: must NOT 500.
    r2 = client_with_mcp_gateway.get("/api/v1/tools")
    assert r2.status_code == 200, r2.text
    names = {item["tool_name"] for item in r2.json()["items"]}
    assert "no_meta" in names
