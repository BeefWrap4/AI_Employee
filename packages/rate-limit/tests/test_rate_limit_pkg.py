"""Tests for shared packages/rate-limit/ package (R25-L)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from ai_employee.rate_limit import (
    InMemoryBackend,
    SlidingWindowLimiter,
    build_sliding_window_limiter,
    install_rate_limiter,
    key_for_request,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Limiter primitives
# --------------------------------------------------------------------------- #


def test_in_memory_backend_basic() -> None:
    be = InMemoryBackend()
    assert be.count("k") == 0
    n = be.add_event("k", 100.0)
    assert n == 1
    assert be.count("k") == 1
    be.trim("k", 200.0)
    assert be.count("k") == 0


def test_sliding_window_limiter_allows_under_limit() -> None:
    limiter = SlidingWindowLimiter(backend=InMemoryBackend(), window_seconds=60, limit=3)
    d1 = limiter.allow("k")
    assert d1.allowed is True
    assert d1.remaining == 2
    d2 = limiter.allow("k")
    assert d2.remaining == 1
    d3 = limiter.allow("k")
    assert d3.remaining == 0
    d4 = limiter.allow("k")
    assert d4.allowed is False
    assert d4.remaining == 0
    assert d4.retry_after_seconds > 0


def test_sliding_window_limiter_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(backend=InMemoryBackend(), window_seconds=60, limit=0)


def test_build_sliding_window_limiter_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "10")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "30")
    limiter = build_sliding_window_limiter()
    assert limiter.limit == 10
    assert limiter.window_seconds == 30


def test_key_for_request_prefers_sub() -> None:
    assert key_for_request("alice", "1.2.3.4") == "sub:alice"
    assert key_for_request(None, "1.2.3.4") == "ip:1.2.3.4"
    assert key_for_request(None, None) == "ip:unknown"


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


@pytest.fixture
def client_with_middleware() -> Iterator[TestClient]:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "1"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    install_rate_limiter(app)
    yield TestClient(app)


@pytest.fixture
def client_with_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "1"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    install_rate_limiter(app)
    yield TestClient(app)


def test_middleware_noop_when_disabled(client_with_middleware: TestClient) -> None:
    # RATE_LIMIT_ENABLED not set → all requests pass
    for _ in range(100):
        r = client_with_middleware.get("/ping")
        assert r.status_code == 200


def test_middleware_health_exempt_when_enabled(client_with_limit: TestClient) -> None:
    for _ in range(5):
        r = client_with_limit.get("/health")
        assert r.status_code == 200


def test_middleware_returns_429_when_enabled(client_with_limit: TestClient) -> None:
    r1 = client_with_limit.get("/ping", headers={"X-User-Id": "alice"})
    assert r1.status_code == 200
    r2 = client_with_limit.get("/ping", headers={"X-User-Id": "alice"})
    assert r2.status_code == 200
    r3 = client_with_limit.get("/ping", headers={"X-User-Id": "alice"})
    assert r3.status_code == 429
    assert r3.headers.get("Retry-After") is not None
    body = r3.json()
    assert body["error"] == "rate_limited"
    assert body["limit"] == 2


def test_middleware_rate_limit_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LIMIT", "5")
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "1"}

    install_rate_limiter(app)
    r = TestClient(app).get("/ping", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "5"
    assert int(r.headers["X-RateLimit-Remaining"]) >= 0
