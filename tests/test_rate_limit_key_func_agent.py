"""R31-A demo: agent-platform-api honors RATE_LIMIT_KEY_FUNC=tenant.

The service calls ``install_rate_limiter(app)`` with no explicit
key_func, so the env var ``RATE_LIMIT_KEY_FUNC`` selects the throttling
dimension.  This test pins the tenant dimension: two distinct users
sharing one ``X-Tenant-ID`` collapse into a single bucket, while a
second tenant gets its own quota.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _tenant_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("RATE_LIMIT_KEY_FUNC", "tenant")


def test_tenant_dimension_buckets_by_tenant_id() -> None:
    client = TestClient(create_app())
    # acme: alice + bob share the tenant bucket (2/2 used)
    assert (
        client.get(
            "/api/v1/audit/events", headers={"X-Tenant-ID": "acme", "X-User-Id": "alice"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/audit/events", headers={"X-Tenant-ID": "acme", "X-User-Id": "bob"}
        ).status_code
        == 200
    )
    # carol under acme → blocked even though she is a new user
    assert (
        client.get(
            "/api/v1/audit/events", headers={"X-Tenant-ID": "acme", "X-User-Id": "carol"}
        ).status_code
        == 429
    )
    # globex has its own fresh bucket
    assert (
        client.get(
            "/api/v1/audit/events", headers={"X-Tenant-ID": "globex", "X-User-Id": "alice"}
        ).status_code
        == 200
    )


def test_unknown_key_func_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized RATE_LIMIT_KEY_FUNC surfaces a clear ValueError at install."""
    monkeypatch.setenv("RATE_LIMIT_KEY_FUNC", "bogus")
    with pytest.raises(ValueError, match="unknown RATE_LIMIT_KEY_FUNC"):
        create_app()
