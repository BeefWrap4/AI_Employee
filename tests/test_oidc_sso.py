"""OIDC SSO tests (spec §8)."""
from __future__ import annotations

import base64
import json
import time

import pytest

from ai_employee.auth_policy.oidc import (
    OIDCClaims,
    OIDCConfig,
    OIDCDisabled,
    OIDCInvalid,
    RemoteJwksClient,
    StubJwksClient,
    build_oidc_verifier,
    verify_oidc_token,
)


def _make_unverified_token(*, iss, aud, exp_offset=3600, sub="alice", extra=None) -> str:
    """Build a structurally valid JWT whose body we control.

    The signature is a dummy — tests pass verify_signature=False so the
    focus stays on claim validation, not RSA crypto.
    """
    header = {"alg": "RS256", "kid": "k1", "typ": "JWT"}
    payload = {
        "iss": iss, "aud": aud, "sub": sub,
        "exp": int(time.time()) + exp_offset,
        "iat": int(time.time()),
        **(extra or {}),
    }

    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}.dummy-signature"


# --------------------------------------------------------------------------- #
# OIDCConfig
# --------------------------------------------------------------------------- #


def test_oidc_config_defaults_disabled() -> None:
    cfg = OIDCConfig()
    assert cfg.enabled is False


def test_oidc_config_enabled_when_issuer_set() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com/realms/acme",
        audience="ai-employee",
        enabled=True,
    )
    assert cfg.enabled is True
    assert cfg.audience == "ai-employee"


def test_oidc_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "ai-employee")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/jwks")
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is True
    assert cfg.issuer == "https://idp.example.com"
    assert cfg.audience == "ai-employee"
    assert cfg.jwks_url == "https://idp.example.com/jwks"


def test_oidc_config_from_env_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is False


def test_oidc_config_requires_audience_to_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is False  # no audience → disabled


# --------------------------------------------------------------------------- #
# JWKS clients
# --------------------------------------------------------------------------- #


def test_stub_jwks_client_returns_keys() -> None:
    keys = [{"kid": "k1", "kty": "RSA", "n": "x", "e": "AQAB"}]
    client = StubJwksClient(keys)
    assert client.fetch() == keys


def test_remote_jwks_client_fetches_via_http() -> None:
    keys = [{"kid": "k1", "kty": "RSA"}]

    class FakeResp:
        status_code = 200

        def json(self):
            return {"keys": keys}

        def raise_for_status(self):
            pass

    captured = {}

    class FakeClient:
        def get(self, url, *, timeout):
            captured["url"] = url
            return FakeResp()

    client = RemoteJwksClient("https://idp/jwks", http_client=FakeClient())
    assert client.fetch() == keys
    assert captured["url"] == "https://idp/jwks"


def test_remote_jwks_client_caches() -> None:
    calls = {"n": 0}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"keys": []}

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, *, timeout):
            calls["n"] += 1
            return FakeResp()

    client = RemoteJwksClient("https://idp/jwks", http_client=FakeClient())
    client.fetch()
    client.fetch()
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# verify_oidc_token (claim validation, signature skipped)
# --------------------------------------------------------------------------- #


def test_verify_oidc_token_valid() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee", enabled=True,
    )
    token = _make_unverified_token(iss="https://idp.example.com", aud="ai-employee")
    claims = verify_oidc_token(token, config=cfg, verify_signature=False)
    assert isinstance(claims, OIDCClaims)
    assert claims.sub == "alice"
    assert claims.iss == "https://idp.example.com"


def test_verify_oidc_token_rejects_wrong_issuer() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee", enabled=True,
    )
    token = _make_unverified_token(iss="https://evil.example.com", aud="ai-employee")
    with pytest.raises(OIDCInvalid, match="issuer"):
        verify_oidc_token(token, config=cfg, verify_signature=False)


def test_verify_oidc_token_rejects_wrong_audience() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee", enabled=True,
    )
    token = _make_unverified_token(iss="https://idp.example.com", aud="other-client")
    with pytest.raises(OIDCInvalid, match="audience"):
        verify_oidc_token(token, config=cfg, verify_signature=False)


def test_verify_oidc_token_accepts_audience_in_list() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee", enabled=True,
    )
    token = _make_unverified_token(
        iss="https://idp.example.com", aud=["other", "ai-employee"],
    )
    claims = verify_oidc_token(token, config=cfg, verify_signature=False)
    assert claims.aud == "other"


def test_verify_oidc_token_rejects_expired() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee",
        enabled=True, clock_skew_s=0,
    )
    token = _make_unverified_token(
        iss="https://idp.example.com", aud="ai-employee", exp_offset=-10,
    )
    with pytest.raises(OIDCInvalid, match="expired"):
        verify_oidc_token(token, config=cfg, verify_signature=False)


def test_verify_oidc_token_respects_clock_skew() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee",
        enabled=True, clock_skew_s=120,
    )
    # Expired 30s ago but within the 120s skew window → still valid.
    token = _make_unverified_token(
        iss="https://idp.example.com", aud="ai-employee", exp_offset=-30,
    )
    claims = verify_oidc_token(token, config=cfg, verify_signature=False)
    assert claims.sub == "alice"


def test_verify_oidc_token_disabled_raises_oidc_disabled() -> None:
    cfg = OIDCConfig(enabled=False)
    with pytest.raises(OIDCDisabled):
        verify_oidc_token("anything", config=cfg, verify_signature=False)


def test_verify_oidc_token_rejects_malformed() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee", enabled=True,
    )
    with pytest.raises(OIDCInvalid, match="malformed"):
        verify_oidc_token("not.a.jwt.token", config=cfg, verify_signature=False)


def test_oidc_claims_extracts_realm_roles() -> None:
    cfg = OIDCConfig(
        issuer="https://idp.example.com", audience="ai-employee", enabled=True,
    )
    token = _make_unverified_token(
        iss="https://idp.example.com", aud="ai-employee",
        extra={"email": "alice@example.com", "realm_access": {"roles": ["ops", "admin"]}},
    )
    claims = verify_oidc_token(token, config=cfg, verify_signature=False)
    assert claims.email == "alice@example.com"
    assert claims.roles == ["ops", "admin"]


def test_oidc_claims_to_dict() -> None:
    claims = OIDCClaims(
        sub="alice", iss="https://idp", aud="ai-employee",
        exp=9999999999, iat=1, email="alice@example.com", roles=["ops"],
        raw={"sub": "alice"},
    )
    d = claims.to_dict()
    assert d["sub"] == "alice"
    assert d["roles"] == ["ops"]


# --------------------------------------------------------------------------- #
# build_oidc_verifier
# --------------------------------------------------------------------------- #


def test_build_oidc_verifier_disabled_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    verifier = build_oidc_verifier(verify_signature=False)
    with pytest.raises(OIDCDisabled):
        verifier.verify("any.token.here")


def test_build_oidc_verifier_enabled_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "ai-employee")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/jwks")
    verifier = build_oidc_verifier(verify_signature=False)
    assert verifier.config.enabled is True
    token = _make_unverified_token(iss="https://idp.example.com", aud="ai-employee")
    claims = verifier.verify(token)
    assert claims.sub == "alice"
