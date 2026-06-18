"""FastAPI auth dependency integration tests."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ai_employee.auth_policy import issue_token
from ai_employee.auth_policy.fastapi_dep import (
    require_internal_or_jwt,
    require_jwt,
)

SECRET = "test-secret-please-rotate-super-long-key-32b"


def _make_app(
    *,
    permissions: list[str] | None = None,
    use_migration: bool = False,
) -> FastAPI:
    app = FastAPI()
    dep = (
        require_internal_or_jwt(permissions)
        if use_migration
        else require_jwt(permissions)
    )

    @app.get("/whoami")
    def whoami(claims=Depends(dep)) -> dict:
        if claims is None:
            return {"subject": "internal-token-trusted"}
        return {"subject": claims.sub, "roles": claims.roles}

    return app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("INTERNAL_TOKEN", "legacy-shared-secret")
    monkeypatch.delenv("JWT_AUTH_STRICT", raising=False)


def test_require_jwt_allows_valid_token() -> None:
    client = TestClient(_make_app(permissions=["knowledge:read"]))
    token = issue_token(
        subject="alice", roles=["viewer"], scopes=["knowledge:read"], secret=SECRET,
    )
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["subject"] == "alice"


def test_require_jwt_denies_missing_token() -> None:
    client = TestClient(_make_app())
    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "authentication_required"


def test_require_jwt_denies_invalid_token() -> None:
    client = TestClient(_make_app())
    resp = client.get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "token_invalid"


def test_require_jwt_forbids_insufficient_permission() -> None:
    client = TestClient(_make_app(permissions=["knowledge:write"]))
    token = issue_token(
        subject="alice", roles=["viewer"], scopes=["knowledge:read"], secret=SECRET,
    )
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "forbidden"


def test_require_jwt_admin_bypasses_permission_check() -> None:
    client = TestClient(_make_app(permissions=["tool:register"]))
    token = issue_token(subject="root", roles=["admin"], secret=SECRET)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_migration_accepts_internal_token() -> None:
    client = TestClient(_make_app(use_migration=True))
    resp = client.get(
        "/whoami", headers={"X-Internal-Token": "legacy-shared-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["subject"] == "internal-token-trusted"


def test_migration_rejects_wrong_internal_token() -> None:
    client = TestClient(_make_app(use_migration=True))
    resp = client.get("/whoami", headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 401


def test_migration_strict_mode_rejects_internal_token(monkeypatch) -> None:
    monkeypatch.setenv("JWT_AUTH_STRICT", "true")
    client = TestClient(_make_app(use_migration=True))
    resp = client.get(
        "/whoami", headers={"X-Internal-Token": "legacy-shared-secret"},
    )
    assert resp.status_code == 401


def test_migration_prefers_jwt_when_both_present() -> None:
    client = TestClient(_make_app(use_migration=True, permissions=["knowledge:read"]))
    token = issue_token(
        subject="alice", roles=["viewer"], scopes=["knowledge:read"], secret=SECRET,
    )
    resp = client.get(
        "/whoami",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Internal-Token": "legacy-shared-secret",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["subject"] == "alice"
