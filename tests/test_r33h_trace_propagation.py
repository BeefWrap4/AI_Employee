"""R33-H: distributed trace_id propagation across service boundaries.

The api-gateway already mints/propagates ``X-Trace-Id`` (and
``X-Run-Id``) to backend services, so inbound requests to
agent-platform-api carry ``X-Trace-Id``.  But the platform's
``HttpApprovalServiceClient`` / ``HttpMcpGatewayClient`` only set
``Content-Type`` + ``X-Internal-Token`` on outbound calls — the trace
chain broke at the platform → service hop.

Cycle 1 (h1): the Http delegating clients now read a context-var
trace context (set via :func:`bind_trace_context`) and add
``X-Trace-Id`` / ``X-Run-Id`` to the outbound headers **when set**.
Outside any ``bind_trace_context`` block the headers are unchanged
(backward compat for tests that assert exact header sets).

Cycle 2 (h2): the platform app binds the trace context from the
inbound request headers (minting a uuid4 when absent) so every
outbound delegation carries a trace_id.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.clients import (
    HttpApprovalServiceClient,
    HttpMcpGatewayClient,
    bind_trace_context,
)
from ai_employee.approval_service.app import create_app as create_approval_app
from ai_employee.approval_service.store import ApprovalTaskStore
from ai_employee.mcp_gateway.app import create_app as create_mcp_app
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Cycle 1: Http delegating clients propagate the context-var trace headers.
# --------------------------------------------------------------------------- #


def test_approval_client_headers_include_trace_id_when_bound() -> None:
    """Within bind_trace_context, HttpApprovalServiceClient adds X-Trace-Id."""
    client = HttpApprovalServiceClient("http://approval-service.test")
    with bind_trace_context("tr-123"):
        headers = client._headers()
    assert headers["X-Trace-Id"] == "tr-123"


def test_mcp_client_headers_include_trace_id_when_bound() -> None:
    """Within bind_trace_context, HttpMcpGatewayClient adds X-Trace-Id."""
    client = HttpMcpGatewayClient("http://mcp-gateway.test")
    with bind_trace_context("tr-456"):
        headers = client._headers()
    assert headers["X-Trace-Id"] == "tr-456"


def test_run_id_propagates_as_x_run_id() -> None:
    """When run_id is bound, both X-Trace-Id and X-Run-Id are present."""
    client = HttpApprovalServiceClient("http://approval-service.test")
    with bind_trace_context("tr-789", run_id="run-abc"):
        headers = client._headers()
    assert headers["X-Trace-Id"] == "tr-789"
    assert headers["X-Run-Id"] == "run-abc"


def test_headers_do_not_include_trace_id_outside_context() -> None:
    """Outside any bind_trace_context, _headers() omits X-Trace-Id (backward compat)."""
    client = HttpApprovalServiceClient("http://approval-service.test")
    headers = client._headers()
    assert "X-Trace-Id" not in headers
    assert "X-Run-Id" not in headers


def test_headers_do_not_include_run_id_when_only_trace_bound() -> None:
    """run_id var unset → no X-Run-Id header (even when trace_id is set)."""
    client = HttpMcpGatewayClient("http://mcp-gateway.test")
    with bind_trace_context("tr-only"):
        headers = client._headers()
    assert headers["X-Trace-Id"] == "tr-only"
    assert "X-Run-Id" not in headers


def test_context_resets_after_block() -> None:
    """After the with-block exits, the trace context is cleared."""
    client = HttpApprovalServiceClient("http://approval-service.test")
    with bind_trace_context("tr-temp"):
        assert client._headers().get("X-Trace-Id") == "tr-temp"
    # Outside the block, no trace header leaks.
    assert "X-Trace-Id" not in client._headers()


def test_approval_decide_sends_trace_id_on_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """decide() puts X-Trace-Id on the outbound httpx.post headers."""
    store = ApprovalTaskStore(db_path=str(tmp_path / "approval.sqlite3"))
    approval_app = create_approval_app(store=store)
    approval_client = TestClient(approval_app)

    captured: dict[str, object] = {}

    def _fake_post(self, path, json):  # type: ignore[no-untyped-def]
        # Capture the headers the client sent.
        captured["headers"] = self._headers()
        return approval_client.post(path, json=json)

    def _fake_get(self, path, params=None):  # type: ignore[no-untyped-def]
        return approval_client.get(path, params=params)

    monkeypatch.setattr(HttpApprovalServiceClient, "_post", _fake_post)
    monkeypatch.setattr(HttpApprovalServiceClient, "_get", _fake_get)
    monkeypatch.setenv("APPROVAL_SERVICE_URL", "http://approval-service.test")

    explicit = HttpApprovalServiceClient("http://approval-service.test")
    platform = TestClient(create_app(approval_client=explicit))

    # Create a run + task.
    resp = platform.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "alice",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert resp.status_code == 201, resp.text
    task = platform.get("/api/v1/approval-tasks?status=pending").json()["items"][0]
    task_id = task["task_id"]

    # Decide WITH a trace context bound — the outbound call must carry it.
    with bind_trace_context("tr-wire-1", run_id="run-wire-1"):
        resp = platform.post(
            f"/api/v1/approval-tasks/{task_id}/decision",
            json={"decision": "approved", "decided_by": "alice", "comment": "ok"},
        )
    assert resp.status_code == 200, resp.text
    sent_headers = captured["headers"]
    assert sent_headers["X-Trace-Id"] == "tr-wire-1"
    assert sent_headers["X-Run-Id"] == "run-wire-1"


def test_mcp_register_sends_trace_id_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register() puts X-Trace-Id on the outbound httpx.post headers."""
    gateway_client = TestClient(create_mcp_app())

    captured: dict[str, object] = {}

    def _fake_post(self, path, json):  # type: ignore[no-untyped-def]
        captured["headers"] = self._headers()
        return gateway_client.post(path, json=json)

    def _fake_get(self, path, params=None):  # type: ignore[no-untyped-def]
        return gateway_client.get(path, params=params)

    monkeypatch.setattr(HttpMcpGatewayClient, "_post", _fake_post)
    monkeypatch.setattr(HttpMcpGatewayClient, "_get", _fake_get)
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway.test")

    explicit = HttpMcpGatewayClient("http://mcp-gateway.test")
    platform = TestClient(create_app(mcp_client=explicit))

    with bind_trace_context("tr-mcp-1"):
        resp = platform.post(
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
    sent_headers = captured["headers"]
    assert sent_headers["X-Trace-Id"] == "tr-mcp-1"


def test_bind_trace_context_none_trace_does_not_add_header() -> None:
    """bind_trace_context(None) must NOT add an empty X-Trace-Id header."""
    client = HttpApprovalServiceClient("http://approval-service.test")
    with bind_trace_context(None):
        headers = client._headers()
    assert "X-Trace-Id" not in headers
    assert "X-Run-Id" not in headers
