"""R32-A: ingress-level single API gateway (spec §三 §5.1).

Spec §5.1 requires a unified platform API gateway that owns
authentication, rate limiting, audit, routing, and trace_id + run_id
propagation.  R25-L + R31-A delivered the shared rate-limit middleware
and the 4-dimension key_func, but each backend service is still exposed
independently — there is no single ingress.  This file pins the
``services/api-gateway`` contract:

* ``GET  /health``                        — liveness probe
* path-prefix routing to the 6 backend services:
    - /api/knowledge/*  → knowledge-api:8010
    - /api/rca/*        → rca-agent:8020
    - /api/platform/*   → agent-platform-api:8030
    - /api/tools/*      → tool-registry:8040
    - /api/approvals/*  → approval-service:8040
    - /api/mcp/*        → mcp-gateway:8050
* unified authentication (JWT / internal-token) — 401 when absent
* unified rate limiting (install_rate_limiter) — 429 when exceeded
* trace_id generation + propagation (X-Trace-Id header) to backend
* audit logging (records trace_id + run_id + method + path + status)

The gateway is a new service; it does not modify any backend.  Tests
inject a stub ``BackendProxy`` so no socket is opened and the routing +
header propagation is asserted hermetically.
"""

from __future__ import annotations

from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _stub_backend() -> dict[str, Any]:
    """Return a stub proxy factory that records every forwarded call.

    The stub mimics the :class:`BackendProxy` Protocol: ``forward`` takes
    the incoming method/path/headers/body and returns a response dict.
    Each call is appended to ``captured`` so tests assert the exact
    upstream URL, method, and propagated headers.
    """
    captured: list[dict[str, Any]] = []

    class _StubProxy:
        def forward(
            self,
            *,
            backend: str,
            method: str,
            path: str,
            headers: dict[str, str],
            body: bytes | None,
        ) -> dict[str, Any]:
            captured.append(
                {
                    "backend": backend,
                    "method": method,
                    "path": path,
                    "headers": dict(headers),
                    "body": body,
                }
            )
            return {
                "status_code": 200,
                "headers": {"Content-Type": "application/json"},
                "body": b'{"ok": true}',
            }

    return {"proxy": _StubProxy(), "captured": captured}


# --------------------------------------------------------------------------- #
# App surface
# --------------------------------------------------------------------------- #


def test_api_gateway_health_endpoint() -> None:
    """``/health`` reports service=api-gateway + status=ok."""
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(backend_proxy=_stub_backend()["proxy"])
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "api-gateway"
    assert body["status"] == "ok"


# --------------------------------------------------------------------------- #
# Routing — each prefix maps to its backend, path is preserved
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "prefix, expected_backend",
    [
        ("/api/knowledge", "knowledge-api"),
        ("/api/rca", "rca-agent"),
        ("/api/platform", "agent-platform-api"),
        ("/api/tools", "tool-registry"),
        ("/api/approvals", "approval-service"),
        ("/api/mcp", "mcp-gateway"),
    ],
)
def test_api_gateway_routes_each_prefix_to_backend(
    prefix: str, expected_backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request under ``/api/<svc>/*`` is forwarded to the right backend."""
    # Auth disabled for routing tests — exercised separately below.
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(f"{prefix}/v1/widgets")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert len(stub["captured"]) == 1
    forwarded = stub["captured"][0]
    assert forwarded["backend"] == expected_backend
    # The backend-relative path is preserved (strip the /api/<svc> prefix).
    assert forwarded["path"] == "/v1/widgets"
    assert forwarded["method"] == "GET"


def test_api_gateway_unknown_prefix_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path not under any known prefix returns 404 (not forwarded)."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get("/api/unknown/v1/widgets")
    assert resp.status_code == 404
    # Nothing was forwarded.
    assert stub["captured"] == []


def test_api_gateway_preserves_method_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST with a JSON body forwards method + body to the backend."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.post("/api/knowledge/v1/docs", json={"title": "doc"})
    assert resp.status_code == 200
    forwarded = stub["captured"][0]
    assert forwarded["method"] == "POST"
    assert b'"title"' in (forwarded["body"] or b"")


# --------------------------------------------------------------------------- #
# Authentication — 401 when missing, pass-through when valid
# --------------------------------------------------------------------------- #


def test_api_gateway_blocks_unauthenticated_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode: a request with no credentials is rejected with 401."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get("/api/knowledge/v1/widgets")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "authentication_required"
    # Nothing was forwarded.
    assert stub["captured"] == []


def test_api_gateway_accepts_internal_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid ``X-Internal-Token`` header authenticates the request."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(
        "/api/knowledge/v1/widgets",
        headers={"X-Internal-Token": "secret"},
    )
    assert resp.status_code == 200, resp.text
    assert len(stub["captured"]) == 1


def test_api_gateway_accepts_valid_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid HS256 JWT (Authorization: Bearer) authenticates the request."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "true")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32-bytes-long-aaaaaa")
    from ai_employee.api_gateway.app import create_app
    from ai_employee.auth_policy.jwt import issue_token
    from fastapi.testclient import TestClient

    token = issue_token(
        subject="user-1", roles=["rca_reader"], secret="test-secret-32-bytes-long-aaaaaa"
    )
    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(
        "/api/knowledge/v1/widgets",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(stub["captured"]) == 1


def test_api_gateway_rejects_invalid_internal_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong ``X-Internal-Token`` is rejected with 401."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(
        "/api/knowledge/v1/widgets",
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 401
    assert stub["captured"] == []


# --------------------------------------------------------------------------- #
# Rate limiting — 429 when exceeded
# --------------------------------------------------------------------------- #


def test_api_gateway_returns_429_when_rate_limit_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-window limit is hit, the gateway responds 429."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    # First two requests succeed.
    r1 = client.get("/api/knowledge/v1/widgets")
    r2 = client.get("/api/knowledge/v1/widgets")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Third exceeds the limit.
    r3 = client.get("/api/knowledge/v1/widgets")
    assert r3.status_code == 429
    assert r3.json()["error"] == "rate_limited"
    # Only the two allowed calls were forwarded.
    assert len(stub["captured"]) == 2


# --------------------------------------------------------------------------- #
# trace_id generation + propagation
# --------------------------------------------------------------------------- #


def test_api_gateway_generates_trace_id_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller sends no X-Trace-Id, the gateway mints one and
    propagates it to the backend."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get("/api/knowledge/v1/widgets")
    assert resp.status_code == 200
    # The response carries the generated trace_id.
    trace_id = resp.headers.get("X-Trace-Id")
    assert trace_id
    assert len(trace_id) >= 8
    # And it was propagated to the backend.
    forwarded = stub["captured"][0]
    assert forwarded["headers"].get("X-Trace-Id") == trace_id


def test_api_gateway_preserves_caller_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller sends an X-Trace-Id, the gateway reuses it."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(
        "/api/knowledge/v1/widgets",
        headers={"X-Trace-Id": "caller-trace-123"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-Id") == "caller-trace-123"
    forwarded = stub["captured"][0]
    assert forwarded["headers"].get("X-Trace-Id") == "caller-trace-123"


def test_api_gateway_propagates_run_id_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``X-Run-Id`` header is forwarded to the backend unchanged."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(
        "/api/platform/v1/agent-runs",
        headers={"X-Run-Id": "run-abc"},
    )
    assert resp.status_code == 200
    forwarded = stub["captured"][0]
    assert forwarded["headers"].get("X-Run-Id") == "run-abc"


# --------------------------------------------------------------------------- #
# Audit logging — records trace_id + run_id + method + path + status
# --------------------------------------------------------------------------- #


def test_api_gateway_records_audit_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each forwarded request produces an audit record with trace_id +
    run_id + method + path + status."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "false")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get(
        "/api/platform/v1/agent-runs",
        headers={"X-Run-Id": "run-audit-1"},
    )
    assert resp.status_code == 200
    trace_id = resp.headers["X-Trace-Id"]

    audit_log = app.state.audit_log
    assert len(audit_log) == 1
    entry = audit_log[0]
    assert entry["trace_id"] == trace_id
    assert entry["run_id"] == "run-audit-1"
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/platform/v1/agent-runs"
    assert entry["backend"] == "agent-platform-api"
    assert entry["status"] == 200


def test_api_gateway_audit_records_rejected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401-rejected request is still audited (status=401)."""
    monkeypatch.setenv("API_GATEWAY_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")
    from ai_employee.api_gateway.app import create_app
    from fastapi.testclient import TestClient

    stub = _stub_backend()
    app = create_app(backend_proxy=stub["proxy"])
    client = TestClient(app)
    resp = client.get("/api/knowledge/v1/widgets")
    assert resp.status_code == 401
    audit_log = app.state.audit_log
    assert len(audit_log) == 1
    assert audit_log[0]["status"] == 401
    assert audit_log[0]["trace_id"]  # still minted
