"""Rate-limit middleware tests (spec §5.1 gateway).

The middleware:
- runs on every request (skipping /health, /docs, etc.)
- keys by X-User-Id header → IP fallback
- returns 429 + Retry-After header on block
- is fully behind the :class:`SlidingWindowLimiter` (test injects a
  permissive or tight limiter via env)
"""

from __future__ import annotations

import os

import pytest
from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _tight_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a 2-per-minute limit so the 429 is observable in <100ms."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")


def test_third_request_returns_429() -> None:
    client = TestClient(create_app())
    headers = {"X-User-Id": "alice"}
    r1 = client.get("/health", headers=headers)
    # /health is excluded — but /api/v1/audit/events is a real endpoint.
    r1 = client.get("/api/v1/audit/events", headers=headers)
    r2 = client.get("/api/v1/audit/events", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    r3 = client.get("/api/v1/audit/events", headers=headers)
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers


def test_health_endpoint_is_exempt() -> None:
    """``/health`` and ``/health/ready`` must never be rate-limited."""
    client = TestClient(create_app())
    for _ in range(5):
        resp = client.get("/health")
        assert resp.status_code == 200


def test_different_users_have_separate_buckets() -> None:
    client = TestClient(create_app())
    # alice exhausts her quota
    client.get("/api/v1/audit/events", headers={"X-User-Id": "alice"})
    client.get("/api/v1/audit/events", headers={"X-User-Id": "alice"})
    blocked = client.get("/api/v1/audit/events", headers={"X-User-Id": "alice"})
    assert blocked.status_code == 429
    # bob still has full quota
    fresh = client.get("/api/v1/audit/events", headers={"X-User-Id": "bob"})
    assert fresh.status_code == 200


def test_rate_limit_disabled_means_no_429() -> None:
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    client = TestClient(create_app())
    headers = {"X-User-Id": "carol"}
    for _ in range(10):
        resp = client.get("/api/v1/audit/events", headers=headers)
        assert resp.status_code == 200


def test_retry_after_header_is_positive() -> None:
    client = TestClient(create_app())
    headers = {"X-User-Id": "dave"}
    client.get("/api/v1/audit/events", headers=headers)
    client.get("/api/v1/audit/events", headers=headers)
    blocked = client.get("/api/v1/audit/events", headers=headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
