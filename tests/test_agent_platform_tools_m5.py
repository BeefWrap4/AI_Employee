from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def _register_tool(
    client: TestClient,
    *,
    tool_name: str,
    service_name: str,
    risk_level: str,
    status: str = "active",
) -> dict:
    response = client.post(
        "/api/v1/tools",
        json={
            "tool_name": tool_name,
            "service_name": service_name,
            "description": f"{tool_name} test tool",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
            "risk_level": risk_level,
            "status": status,
            "health_check_url": f"http://{service_name}/health",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_register_tool_and_list_tools() -> None:
    client = TestClient(create_app())

    created = _register_tool(
        client,
        tool_name="knowledge-api.chat.query",
        service_name="knowledge-api",
        risk_level="read_only",
    )

    assert created["tool_name"] == "knowledge-api.chat.query"
    assert created["status"] == "active"
    assert created["risk_level"] == "read_only"
    assert created["health_status"] == "unknown"

    listed = client.get("/api/v1/tools")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["tool_name"] == "knowledge-api.chat.query"


def test_list_tools_filters_by_risk_status_and_service() -> None:
    client = TestClient(create_app())
    _register_tool(
        client,
        tool_name="knowledge-api.chat.query",
        service_name="knowledge-api",
        risk_level="read_only",
    )
    _register_tool(
        client,
        tool_name="rca-agent.ticket.writeback",
        service_name="rca-agent",
        risk_level="approval_required",
        status="disabled",
    )

    filtered = client.get(
        "/api/v1/tools?risk_level=approval_required&status=disabled&service_name=rca-agent"
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    item = filtered.json()["items"][0]
    assert item["tool_name"] == "rca-agent.ticket.writeback"
    assert item["risk_level"] == "approval_required"
    assert item["status"] == "disabled"


def test_register_duplicate_tool_rejected() -> None:
    client = TestClient(create_app())
    _register_tool(
        client,
        tool_name="knowledge-api.chat.query",
        service_name="knowledge-api",
        risk_level="read_only",
    )

    duplicate = client.post(
        "/api/v1/tools",
        json={
            "tool_name": "knowledge-api.chat.query",
            "service_name": "knowledge-api",
            "description": "duplicate",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
            "status": "active",
        },
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["error_code"] == "tool_already_registered"
