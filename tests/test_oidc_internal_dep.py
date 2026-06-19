"""Tests for :func:`require_oidc_or_internal` (R24-A.3).

Covers the resolution order:

  1. OIDC enabled  + Bearer token  → OIDC claims (RS256 verification).
  2. OIDC disabled + Bearer JWT    → legacy HS256 JWT.
  3. OIDC disabled + no Bearer     → ``X-Internal-Token`` accepted.
  4. Strict mode                   → internal token rejected.
  5. No credentials at all         → 401.
"""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from ai_employee.auth_policy import issue_token
from ai_employee.auth_policy.fastapi_dep import (
    OIDCOrInternalPrincipal,
    require_oidc_or_internal,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

SECRET = "test-secret-please-rotate-super-long-key-32b"
INTERNAL = "legacy-internal-token"


# --------------------------------------------------------------------------- #
# RSA keypair + JWKS helper (mirrors test_oidc_signature)
# --------------------------------------------------------------------------- #


def _rsa_keypair() -> tuple[Any, Any]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _public_jwk(public_key: Any, *, kid: str) -> dict[str, Any]:
    import base64

    nums = public_key.public_numbers()

    def b64u(value: int) -> str:
        length = (value.bit_length() + 7) // 8 or 1
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64u(nums.n),
        "e": b64u(nums.e),
    }


def _oidc_token(
    *,
    private_key: Any,
    kid: str,
    iss: str,
    aud: str,
    sub: str = "alice",
    roles: list[str] | None = None,
) -> str:
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": now + 3600,
    }
    if roles:
        payload["realm_access"] = {"roles": roles}
    return pyjwt.encode(
        payload,
        pem,
        algorithm="RS256",
        headers={"kid": kid, "typ": "JWT"},
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("INTERNAL_TOKEN", INTERNAL)
    monkeypatch.delenv("JWT_AUTH_STRICT", raising=False)
    # OIDC defaults: disabled
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


def _build_app(perms: list[str] | None = None) -> FastAPI:
    app = FastAPI()
    dep = require_oidc_or_internal(perms)

    @app.get("/whoami")
    def whoami(principal: OIDCOrInternalPrincipal = Depends(dep)) -> dict:
        return {
            "kind": principal.kind,
            "sub": principal.subject(),
            "roles": principal.roles(),
        }

    return app


# --------------------------------------------------------------------------- #
# 1. Legacy HS256 JWT path
# --------------------------------------------------------------------------- #


def test_falls_back_to_hs256_jwt_when_oidc_disabled() -> None:
    client = TestClient(_build_app(perms=["knowledge:read"]))
    token = issue_token(
        subject="alice",
        roles=["viewer"],
        scopes=["knowledge:read"],
        secret=SECRET,
    )
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "jwt"
    assert body["sub"] == "alice"
    assert "viewer" in body["roles"]


# --------------------------------------------------------------------------- #
# 2. Internal-token path (legacy fallback)
# --------------------------------------------------------------------------- #


def test_accepts_internal_token_when_no_bearer() -> None:
    client = TestClient(_build_app())
    resp = client.get("/whoami", headers={"X-Internal-Token": INTERNAL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "internal"
    assert body["sub"] == "internal-token-trusted"


def test_strict_mode_rejects_internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_AUTH_STRICT", "true")
    client = TestClient(_build_app())
    resp = client.get("/whoami", headers={"X-Internal-Token": INTERNAL})
    assert resp.status_code == 401


def test_wrong_internal_token_rejected() -> None:
    client = TestClient(_build_app())
    resp = client.get("/whoami", headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 401


def test_no_credentials_rejected() -> None:
    client = TestClient(_build_app())
    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "authentication_required"


# --------------------------------------------------------------------------- #
# 3. Permission enforcement
# --------------------------------------------------------------------------- #


def test_jwt_path_enforces_permissions() -> None:
    client = TestClient(_build_app(perms=["tool:register"]))
    token = issue_token(
        subject="alice",
        roles=["viewer"],
        scopes=["knowledge:read"],
        secret=SECRET,
    )
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "forbidden"


def test_internal_token_bypasses_permission_check() -> None:
    client = TestClient(_build_app(perms=["tool:register"]))
    resp = client.get("/whoami", headers={"X-Internal-Token": INTERNAL})
    assert resp.status_code == 200


def test_jwt_admin_role_passes_any_permission() -> None:
    client = TestClient(_build_app(perms=["tool:register"]))
    token = issue_token(subject="root", roles=["admin"], secret=SECRET)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 4. OIDC enabled + valid RS256 token
# --------------------------------------------------------------------------- #


def test_oidc_enabled_with_valid_token_uses_oidc_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, pub = _rsa_keypair()
    kid = "oidc-test"
    jwks = [_public_jwk(pub, kid=kid)]
    iss = "https://idp.example.com/realms/acme"
    aud = "ai-employee"
    monkeypatch.setenv("OIDC_ISSUER", iss)
    monkeypatch.setenv("OIDC_CLIENT_ID", aud)
    monkeypatch.setenv("OIDC_AUDIENCE", aud)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/jwks")
    # Patch the verifier to use our static JWKS rather than a real fetch.
    from ai_employee.auth_policy import fastapi_dep as dep_mod
    from ai_employee.auth_policy.oidc import OIDCConfig, OIDCVerifier

    cfg = OIDCConfig(
        issuer=iss,
        audience=aud,
        jwks_url="https://idp.example.com/jwks",
        enabled=True,
    )
    verifier = OIDCVerifier(cfg, _StaticJwks(jwks), verify_signature=True)
    monkeypatch.setattr(dep_mod, "build_oidc_verifier", lambda **kw: verifier)
    # Reset the cached verifier: build_oidc_verifier is called each request,
    # so the patch above suffices.
    token = _oidc_token(
        private_key=priv,
        kid=kid,
        iss=iss,
        aud=aud,
        sub="alice",
        roles=["viewer", "ops"],
    )
    client = TestClient(_build_app(perms=["knowledge:read"]))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "oidc"
    assert body["sub"] == "alice"
    assert "ops" in body["roles"]


def test_oidc_enabled_rejects_tampered_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, pub = _rsa_keypair()
    kid = "oidc-test"
    jwks = [_public_jwk(pub, kid=kid)]
    iss = "https://idp.example.com/realms/acme"
    aud = "ai-employee"
    monkeypatch.setenv("OIDC_ISSUER", iss)
    monkeypatch.setenv("OIDC_CLIENT_ID", aud)
    monkeypatch.setenv("OIDC_AUDIENCE", aud)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/jwks")
    from ai_employee.auth_policy import fastapi_dep as dep_mod
    from ai_employee.auth_policy.oidc import OIDCConfig, OIDCVerifier

    cfg = OIDCConfig(
        issuer=iss,
        audience=aud,
        jwks_url="https://idp.example.com/jwks",
        enabled=True,
    )
    verifier = OIDCVerifier(cfg, _StaticJwks(jwks), verify_signature=True)
    monkeypatch.setattr(dep_mod, "build_oidc_verifier", lambda **kw: verifier)
    # Sign a valid token, then mutate the payload to forge `sub`.
    import base64
    import json

    token = _oidc_token(
        private_key=priv,
        kid=kid,
        iss=iss,
        aud=aud,
        sub="alice",
    )
    parts = token.split(".")
    raw = parts[1]
    pad = "=" * (-len(raw) % 4)
    payload = json.loads(base64.urlsafe_b64decode(raw + pad))
    payload["sub"] = "admin"
    tampered = (
        parts[0]
        + "."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        + "."
        + parts[2]
    )
    client = TestClient(_build_app())
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "oidc_invalid"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StaticJwks:
    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self._keys = list(keys)

    def fetch(self, kid: str | None = None) -> list[dict[str, Any]]:
        return list(self._keys)
