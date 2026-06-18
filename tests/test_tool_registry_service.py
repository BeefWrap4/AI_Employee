"""tool-registry service API tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_employee.auth_policy import issue_token
from ai_employee.tool_registry.app import create_app
from ai_employee.tool_registry.store import ToolRegistryStore

SECRET = "test-secret-please-rotate-super-long-key-32b"


def _client(tmp_path) -> TestClient:
    store = ToolRegistryStore(db_path=str(tmp_path / "tools.sqlite3"))
    return TestClient(create_app(store=store))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("INTERNAL_TOKEN", "legacy-shared-secret")
    monkeypatch.delenv("JWT_AUTH_STRICT", raising=False)


def _admin_headers() -> dict[str, str]:
    token = issue_token(subject="root", roles=["admin"], secret=SECRET)
    return {"Authorization": f"Bearer {token}"}


def _operator_headers() -> dict[str, str]:
    token = issue_token(
        subject="alice", roles=["operator"], scopes=["tool:invoke"], secret=SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


def test_health(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "tool-registry"


def test_list_tools_returns_builtin_echo_and_mcp_shape(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/v1/tools")
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["tools"]}
    assert "echo" in names
    echo = next(t for t in body["tools"] if t["name"] == "echo")
    assert echo["inputSchema"]["type"] == "object"
    assert echo["metadata"]["risk_level"] == "read_only"


def test_register_tool_requires_auth(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools",
        json={
            "name": "demo.lookup",
            "description": "lookup",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
    )
    assert resp.status_code == 401


def test_register_tool_forbidden_for_operator(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools",
        json={
            "name": "demo.lookup",
            "description": "lookup",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
        headers=_operator_headers(),
    )
    assert resp.status_code == 403


def test_register_tool_succeeds_for_admin(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools",
        json={
            "name": "demo.lookup",
            "description": "lookup a cell",
            "input_schema": {
                "type": "object",
                "properties": {"cell_id": {"type": "string"}},
                "required": ["cell_id"],
            },
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
            "service_name": "knowledge-api",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["registered"] is True

    # Tool is persisted and appears in list.
    listing = client.get("/api/v1/tools")
    names = {t["name"] for t in listing.json()["tools"]}
    assert "demo.lookup" in names

    # GET single tool returns the spec.
    fetched = client.get("/api/v1/tools/demo.lookup")
    assert fetched.status_code == 200
    assert fetched.json()["service_name"] == "knowledge-api"


def test_register_rejects_invalid_risk_level(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools",
        json={
            "name": "bad.risk",
            "description": "x",
            "input_schema": {},
            "output_schema": {},
            "risk_level": "nuclear",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_risk_level"


def test_invoke_builtin_echo_with_operator(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools/echo/invoke",
        json={"arguments": {"text": "hello"}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"echo": "hello"}
    assert resp.json()["invoked_by"] == "alice"


def test_invoke_requires_auth(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools/echo/invoke",
        json={"arguments": {"text": "hello"}},
    )
    assert resp.status_code == 401


def test_invoke_unknown_tool_returns_404(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools/does-not-exist/invoke",
        json={"arguments": {}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 404


def test_invoke_declaratively_registered_tool_without_handler_409(tmp_path) -> None:
    client = _client(tmp_path)
    # Register declaratively (no handler) as admin.
    client.post(
        "/api/v1/tools",
        json={
            "name": "decl.only",
            "description": "no handler",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
        },
        headers=_admin_headers(),
    )
    resp = client.post(
        "/api/v1/tools/decl.only/invoke",
        json={"arguments": {}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "tool_not_invokable"


def test_invoke_high_risk_tool_requires_admin(tmp_path) -> None:
    client = _client(tmp_path)
    client.post(
        "/api/v1/tools",
        json={
            "name": "danger.zone",
            "description": "high risk",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "high_risk",
        },
        headers=_admin_headers(),
    )
    # Operator cannot invoke high_risk.
    resp = client.post(
        "/api/v1/tools/danger.zone/invoke",
        json={"arguments": {}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 403
    # Admin also cannot invoke (no handler → 409, not 403), which proves
    # the permission check passed for admin.
    resp2 = client.post(
        "/api/v1/tools/danger.zone/invoke",
        json={"arguments": {}},
        headers=_admin_headers(),
    )
    assert resp2.status_code == 409


def test_internal_token_migration_allows_register(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/tools",
        json={
            "name": "via.token",
            "description": "x",
            "input_schema": {},
            "output_schema": {},
            "risk_level": "read_only",
        },
        headers={"X-Internal-Token": "legacy-shared-secret"},
    )
    assert resp.status_code == 201
    assert resp.json()["registered_by"] == "internal"


def test_persistence_survives_restart(tmp_path) -> None:
    db_path = str(tmp_path / "tools.sqlite3")
    store = ToolRegistryStore(db_path=db_path)
    client = TestClient(create_app(store=store))
    client.post(
        "/api/v1/tools",
        json={
            "name": "persisted.tool",
            "description": "survives restart",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
            "service_name": "rca-agent",
        },
        headers=_admin_headers(),
    )
    # New store + app instance reading the same DB file.
    store2 = ToolRegistryStore(db_path=db_path)
    client2 = TestClient(create_app(store=store2))
    listing = client2.get("/api/v1/tools")
    names = {t["name"] for t in listing.json()["tools"]}
    assert "persisted.tool" in names
