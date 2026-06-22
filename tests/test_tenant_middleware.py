"""Tenant middleware + whoami endpoint tests."""

from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def test_whoami_returns_default_tenant_without_header() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/tenant/whoami")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "public"
    assert body["source"] in {"default", "header", "query", "subject"}


def test_whoami_respects_x_tenant_id_header() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/tenant/whoami", headers={"X-Tenant-ID": "acme"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "acme"
    assert body["source"] == "header"


def test_whoami_query_param_wins_over_header() -> None:
    client = TestClient(create_app())
    resp = client.get(
        "/api/v1/tenant/whoami?tenant_id=acme",
        headers={"X-Tenant-ID": "globex"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "acme"
    assert body["source"] == "query"


def test_whoami_rejects_invalid_tenant_id() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/tenant/whoami", headers={"X-Tenant-ID": "bad tenant!"})
    assert resp.status_code == 400


def test_whoami_rejects_overlong_tenant_id() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/tenant/whoami", headers={"X-Tenant-ID": "a" * 100})
    assert resp.status_code == 400


def test_audit_event_records_tenant_id() -> None:
    """When a request runs with an X-Tenant-ID header, the resulting audit
    event carries that tenant_id (when produced by the request path).
    """
    from ai_employee.agent_platform_api.audit import audit_log, reset_audit_log

    reset_audit_log()
    client = TestClient(create_app())
    # Create a run with tenant context.
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "x"},
        },
        headers={"X-Tenant-ID": "acme"},
    )
    assert resp.status_code == 201
    events = audit_log().list_by_action("run.created")
    assert events
    payload = events[-1].payload
    assert payload.get("tenant_id") == "acme"


def test_audit_event_default_tenant_when_no_header() -> None:
    from ai_employee.agent_platform_api.audit import audit_log, reset_audit_log

    reset_audit_log()
    client = TestClient(create_app())
    client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "x"},
        },
    )
    events = audit_log().list_by_action("run.created")
    assert events
    payload = events[-1].payload
    assert payload.get("tenant_id") == "public"
