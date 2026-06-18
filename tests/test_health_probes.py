"""Readiness vs liveness probe tests.

Liveness = "process is alive" (cheap, always ok unless the process is
about to exit).  Readiness = "downstream deps are reachable and the
service can serve traffic" (probes DB / Redis / downstream services).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_employee.agent_platform_api.health import (
    DependencyCheck,
    ReadinessResult,
    check_sqlite,
    check_redis,
)


# --------------------------------------------------------------------------- #
# DependencyCheck
# --------------------------------------------------------------------------- #


def test_dependency_check_ok() -> None:
    dep = DependencyCheck(name="db", healthy=True, latency_ms=2.1)
    assert dep.healthy is True
    assert dep.error is None


def test_dependency_check_unhealthy() -> None:
    dep = DependencyCheck(name="db", healthy=False, latency_ms=0.0, error="conn refused")
    assert dep.healthy is False
    assert dep.error == "conn refused"


def test_dependency_check_to_dict() -> None:
    dep = DependencyCheck(name="db", healthy=True, latency_ms=1.5)
    d = dep.to_dict()
    assert d == {"name": "db", "healthy": True, "latency_ms": 1.5, "error": None}


# --------------------------------------------------------------------------- #
# ReadinessResult
# --------------------------------------------------------------------------- #


def test_readiness_result_all_healthy_is_ready() -> None:
    result = ReadinessResult(
        checks=[
            DependencyCheck(name="db", healthy=True, latency_ms=1.0),
            DependencyCheck(name="redis", healthy=True, latency_ms=0.5),
        ],
    )
    assert result.ready is True
    assert result.unhealthy == []


def test_readiness_result_one_unhealthy_is_not_ready() -> None:
    result = ReadinessResult(
        checks=[
            DependencyCheck(name="db", healthy=True, latency_ms=1.0),
            DependencyCheck(name="redis", healthy=False, latency_ms=0.0, error="down"),
        ],
    )
    assert result.ready is False
    assert result.unhealthy == ["redis"]


def test_readiness_result_empty_is_ready() -> None:
    """No deps configured → trivially ready."""
    result = ReadinessResult(checks=[])
    assert result.ready is True


def test_readiness_result_to_dict() -> None:
    result = ReadinessResult(
        checks=[DependencyCheck(name="db", healthy=True, latency_ms=1.0)],
    )
    d = result.to_dict()
    assert d["ready"] is True
    assert len(d["checks"]) == 1
    assert d["unhealthy"] == []


# --------------------------------------------------------------------------- #
# check_sqlite / check_redis
# --------------------------------------------------------------------------- #


def test_check_sqlite_ok(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    dep = check_sqlite(str(db_path))
    assert dep.healthy is True
    assert dep.latency_ms >= 0
    assert dep.error is None


def test_check_sqlite_missing_file(tmp_path) -> None:
    dep = check_sqlite(str(tmp_path / "missing.sqlite3"))
    assert dep.healthy is False
    assert dep.error is not None


def test_check_redis_unreachable_returns_unhealthy() -> None:
    dep = check_redis("redis://127.0.0.1:1/0", timeout_s=0.1)
    assert dep.healthy is False
    assert dep.error is not None


# --------------------------------------------------------------------------- #
# Endpoint behaviour
# --------------------------------------------------------------------------- #


def test_liveness_endpoint_always_ok() -> None:
    client = TestClient(create_agent_platform_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_readiness_endpoint_returns_200_when_no_deps() -> None:
    """With no deps configured, /health/ready is 200 + ready=True."""
    client = TestClient(create_agent_platform_app())
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert isinstance(body["checks"], list)


def test_readiness_endpoint_503_when_dep_unhealthy(monkeypatch) -> None:
    """If a dep check fails, /health/ready returns 503 so k8s 摘流."""
    from ai_employee.agent_platform_api import health as health_mod

    def fake_check_sqlite(path: str):
        return health_mod.DependencyCheck(
            name="sqlite", healthy=False, latency_ms=0.0, error="boom",
        )

    monkeypatch.setattr(health_mod, "check_sqlite", fake_check_sqlite)
    monkeypatch.setenv("SQLITE_PATH", "/tmp/whatever.sqlite3")
    client = TestClient(create_agent_platform_app())
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert "sqlite" in body["unhealthy"]


def test_readiness_endpoint_200_when_dep_healthy(tmp_path, monkeypatch) -> None:
    import sqlite3

    db_path = tmp_path / "ok.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    client = TestClient(create_agent_platform_app())
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def create_agent_platform_app():
    from ai_employee.agent_platform_api.app import create_app

    return create_app()
