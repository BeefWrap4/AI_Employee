"""R31-A: rate-limit key_func dimension tests.

``install_rate_limiter`` gains an optional ``key_func(request) -> str``
parameter plus four built-in factories:

  * ``key_by_user``      — X-User-Id → IP (default, backward compatible)
  * ``key_by_tenant``    — X-Tenant-ID header (multi-tenant isolation)
  * ``key_by_endpoint``  — path + IP (per-endpoint throttling)
  * ``key_by_tool``      — tool_name + user for ``/tools/{name}/invoke``

Each test drives a tight 2-per-minute limiter and asserts the chosen
dimension isolates (or shares) buckets as expected.
"""

from __future__ import annotations

import pytest
from ai_employee.rate_limit import (
    install_rate_limiter,
    key_by_endpoint,
    key_by_tenant,
    key_by_tool,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _tight_app(*, key_func=None) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "1"}

    @app.get("/other")
    def other() -> dict[str, str]:
        return {"other": "1"}

    @app.post("/tools/{name}/invoke")
    def invoke_tool(name: str) -> dict[str, str]:
        return {"invoked": name}

    install_rate_limiter(app, key_func=key_func)
    return app


@pytest.fixture(autouse=True)
def _tight_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")


def test_default_key_func_preserves_user_id_behavior() -> None:
    """No key_func == key_by_user: X-User-Id isolates, IP falls back."""
    client = TestClient(_tight_app())
    # alice exhausts her 2-per-min quota on /ping
    assert client.get("/ping", headers={"X-User-Id": "alice"}).status_code == 200
    assert client.get("/ping", headers={"X-User-Id": "alice"}).status_code == 200
    assert client.get("/ping", headers={"X-User-Id": "alice"}).status_code == 429
    # bob is a separate bucket
    assert client.get("/ping", headers={"X-User-Id": "bob"}).status_code == 200


def test_key_by_tenant_isolates_buckets() -> None:
    """key_by_tenant buckets by X-Tenant-ID; users in same tenant share."""
    client = TestClient(_tight_app(key_func=key_by_tenant))
    # tenant-acme: two different users share the tenant bucket
    assert (
        client.get("/ping", headers={"X-Tenant-ID": "acme", "X-User-Id": "alice"}).status_code
        == 200
    )
    assert (
        client.get("/ping", headers={"X-Tenant-ID": "acme", "X-User-Id": "bob"}).status_code == 200
    )
    # third request under same tenant → blocked regardless of user
    assert (
        client.get("/ping", headers={"X-Tenant-ID": "acme", "X-User-Id": "carol"}).status_code
        == 429
    )
    # a different tenant has its own fresh bucket
    assert (
        client.get("/ping", headers={"X-Tenant-ID": "globex", "X-User-Id": "alice"}).status_code
        == 200
    )


def test_key_by_endpoint_isolates_paths() -> None:
    """key_by_endpoint buckets by path + IP: same IP on different paths do not interfere."""
    client = TestClient(_tight_app(key_func=key_by_endpoint))
    # same IP exhausts /ping
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
    # same IP still has full quota on /other
    assert client.get("/other").status_code == 200
    assert client.get("/other").status_code == 200


def test_key_by_tool_isolates_tool_invokes() -> None:
    """key_by_tool buckets by tool_name + user on /tools/{name}/invoke paths."""
    client = TestClient(_tight_app(key_func=key_by_tool))
    h = {"X-User-Id": "alice"}
    # toolA exhausted for alice
    assert client.post("/tools/toolA/invoke", headers=h).status_code == 200
    assert client.post("/tools/toolA/invoke", headers=h).status_code == 200
    assert client.post("/tools/toolA/invoke", headers=h).status_code == 429
    # toolB is a separate bucket for the same user
    assert client.post("/tools/toolB/invoke", headers=h).status_code == 200
    # toolA for a different user is also separate
    assert client.post("/tools/toolA/invoke", headers={"X-User-Id": "bob"}).status_code == 200


def test_custom_key_func_callable() -> None:
    """A user-supplied callable is invoked and its return value buckets requests."""
    seen: list[str] = []

    def custom(request) -> str:
        val = f"custom:{request.headers.get('X-Team', 'none')}"
        seen.append(val)
        return val

    client = TestClient(_tight_app(key_func=custom))
    assert client.get("/ping", headers={"X-Team": "red"}).status_code == 200
    assert client.get("/ping", headers={"X-Team": "red"}).status_code == 200
    assert client.get("/ping", headers={"X-Team": "red"}).status_code == 429
    assert client.get("/ping", headers={"X-Team": "blue"}).status_code == 200
    assert "custom:red" in seen
    assert "custom:blue" in seen
