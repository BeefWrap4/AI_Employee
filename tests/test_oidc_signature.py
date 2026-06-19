"""OIDC RS256 signature verification tests (R24-A.1).

Exercises the end-to-end signature path:

  * generate a real RS256 keypair with ``cryptography``;
  * export the public key as JWKS;
  * sign a JWT with the private key;
  * verify it via :func:`verify_oidc_token` + the JWKS-backed verifier;
  * mutate a claim after signing and assert the mutation is rejected.

These tests use the real ``pyjwt`` + ``cryptography`` stack — no
``verify_signature=False`` shortcuts — so any regression that degrades
``_verify_signature`` to a structural check (e.g. only validating that
``PyJWK`` can parse the key) trips at least one assertion here.
"""
from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from ai_employee.auth_policy.oidc import (
    OIDCConfig,
    OIDCInvalid,
    RemoteJwksClient,
    verify_oidc_token,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# --------------------------------------------------------------------------- #
# Test fixtures — real RS256 key + JWKS
# --------------------------------------------------------------------------- #


def _generate_rsa_keypair() -> tuple[Any, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _public_jwk(public_key: Any, *, kid: str = "test-kid-1") -> dict[str, Any]:
    """Convert a cryptography public key into a JWK dict for the JWKS."""
    numbers = public_key.public_numbers()
    import base64

    def b64u(value: int) -> str:
        # Encode unsigned int as big-endian, base64url, no padding.
        length = (value.bit_length() + 7) // 8 or 1
        raw = value.to_bytes(length, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64u(numbers.n),
        "e": b64u(numbers.e),
    }


def _sign_rs256(
    *,
    private_key: Any,
    kid: str,
    iss: str,
    aud: str,
    sub: str = "alice",
    exp_offset: int = 3600,
    extra: dict[str, Any] | None = None,
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
        "exp": now + exp_offset,
    }
    if extra:
        payload.update(extra)
    return pyjwt.encode(
        payload,
        pem,
        algorithm="RS256",
        headers={"kid": kid, "typ": "JWT"},
    )


@pytest.fixture
def rsa_setup() -> dict[str, Any]:
    """Return a dict with the private key, public key, JWKS, and config."""
    private_key, public_key = _generate_rsa_keypair()
    kid = "rsa-test-kid"
    jwks = [_public_jwk(public_key, kid=kid)]
    cfg = OIDCConfig(
        issuer="https://idp.example.com/realms/acme",
        audience="ai-employee",
        jwks_url="https://idp.example.com/realms/acme/protocol/openid-connect/certs",
        enabled=True,
        clock_skew_s=60,
    )
    return {
        "private_key": private_key,
        "public_key": public_key,
        "kid": kid,
        "jwks": jwks,
        "config": cfg,
    }


# --------------------------------------------------------------------------- #
# End-to-end: real signature path
# --------------------------------------------------------------------------- #


def test_real_rs256_signature_is_accepted(rsa_setup: dict[str, Any]) -> None:
    """A properly signed token must verify under real RS256 signature check."""
    token = _sign_rs256(
        private_key=rsa_setup["private_key"],
        kid=rsa_setup["kid"],
        iss=rsa_setup["config"].issuer,
        aud=rsa_setup["config"].audience,
    )

    claims = verify_oidc_token(
        token,
        config=rsa_setup["config"],
        jwks_client=_StaticJwks(rsa_setup["jwks"]),
        verify_signature=True,
    )
    assert claims.sub == "alice"
    assert claims.iss == rsa_setup["config"].issuer
    assert claims.aud == rsa_setup["config"].audience


def test_tampered_payload_is_rejected(rsa_setup: dict[str, Any]) -> None:
    """Mutating a claim after signing must invalidate the RS256 signature."""
    token = _sign_rs256(
        private_key=rsa_setup["private_key"],
        kid=rsa_setup["kid"],
        iss=rsa_setup["config"].issuer,
        aud=rsa_setup["config"].audience,
        sub="alice",
    )
    # Tamper: swap the `sub` claim to `admin` *after* signing.  Re-encoding
    # the payload while keeping the original signature must fail RS256
    # verification.
    import base64
    import json

    header_b64, _, sig_b64 = token.split(".")
    parts = token.split(".")
    raw = parts[1]
    pad = "=" * (-len(raw) % 4)
    payload = json.loads(base64.urlsafe_b64decode(raw + pad))
    payload["sub"] = "admin"
    tampered_payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode())
        .rstrip(b"=")
        .decode()
    )
    tampered = f"{header_b64}.{tampered_payload_b64}.{sig_b64}"
    assert tampered != token  # sanity

    with pytest.raises(OIDCInvalid):
        verify_oidc_token(
            tampered,
            config=rsa_setup["config"],
            jwks_client=_StaticJwks(rsa_setup["jwks"]),
            verify_signature=True,
        )


def test_signature_signed_with_wrong_key_is_rejected(
    rsa_setup: dict[str, Any],
) -> None:
    """A token signed by a *different* private key must be rejected."""
    other_private, _ = _generate_rsa_keypair()
    token = _sign_rs256(
        private_key=other_private,
        kid=rsa_setup["kid"],  # claim the same kid to bypass kid lookup
        iss=rsa_setup["config"].issuer,
        aud=rsa_setup["config"].audience,
    )
    with pytest.raises(OIDCInvalid):
        verify_oidc_token(
            token,
            config=rsa_setup["config"],
            jwks_client=_StaticJwks(rsa_setup["jwks"]),
            verify_signature=True,
        )


def test_unknown_kid_is_rejected(rsa_setup: dict[str, Any]) -> None:
    """A token with an unknown ``kid`` must be rejected with OIDCInvalid."""
    token = _sign_rs256(
        private_key=rsa_setup["private_key"],
        kid="not-in-jwks",
        iss=rsa_setup["config"].issuer,
        aud=rsa_setup["config"].audience,
    )
    with pytest.raises(OIDCInvalid, match="kid"):
        verify_oidc_token(
            token,
            config=rsa_setup["config"],
            jwks_client=_StaticJwks(rsa_setup["jwks"]),
            verify_signature=True,
        )


# --------------------------------------------------------------------------- #
# RemoteJwksClient.refresh-on-kid-miss
# --------------------------------------------------------------------------- #


def test_remote_jwks_client_refreshes_on_kid_miss() -> None:
    """When the cached JWKS doesn't contain the requested kid, refresh once."""
    initial = [{"kid": "old", "kty": "RSA", "n": "x", "e": "AQAB"}]
    rotated = [
        {"kid": "old", "kty": "RSA", "n": "x", "e": "AQAB"},
        {"kid": "new", "kty": "RSA", "n": "y", "e": "AQAB"},
    ]
    responses = [initial, rotated]

    class FakeResp:
        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body

        def json(self) -> dict[str, Any]:
            return self._body

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, *, timeout: float):  # type: ignore[no-untyped-def]
            idx = min(self.calls, len(responses) - 1)
            self.calls += 1
            return FakeResp({"keys": responses[idx]})

    fake = FakeClient()
    client = RemoteJwksClient(
        "https://idp/jwks",
        http_client=fake,
        jwks_ttl_s=3600,
    )
    # Warm the cache (no kid → returns `initial`).
    warm = client.fetch()
    assert any(k["kid"] == "old" for k in warm)
    assert fake.calls == 1
    # Now request a kid not present in the cached set — should trigger a
    # refresh, returning the rotated set with `new` included.
    keys1 = client.fetch(kid="new")
    assert any(k["kid"] == "new" for k in keys1)
    assert fake.calls == 2  # warm + refresh on miss
    # Second call for `new` — now cached, no extra fetch.
    keys2 = client.fetch(kid="new")
    assert any(k["kid"] == "new" for k in keys2)
    assert fake.calls == 2


def test_remote_jwks_client_respects_ttl() -> None:
    """Expired TTL forces a refetch even when the cached kid is present."""
    bodies = [
        {"keys": [{"kid": "k1", "kty": "RSA", "n": "x", "e": "AQAB"}]},
        {"keys": [{"kid": "k1", "kty": "RSA", "n": "y", "e": "AQAB"}]},
    ]
    states = {"idx": 0, "calls": 0}

    class FakeResp:
        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body

        def json(self) -> dict[str, Any]:
            return self._body

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        def get(self, url: str, *, timeout: float):  # type: ignore[no-untyped-def]
            states["calls"] += 1
            return FakeResp(bodies[min(states["idx"], len(bodies) - 1)])

    fake = FakeClient()
    client = RemoteJwksClient(
        "https://idp/jwks",
        http_client=fake,
        jwks_ttl_s=0,  # immediate expiry
    )
    keys1 = client.fetch()
    keys2 = client.fetch()
    assert states["calls"] == 2  # TTL=0 means every fetch refetches


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StaticJwks:
    """Adapts an in-memory key set to the JwksClient protocol."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self._keys = list(keys)

    def fetch(self, kid: str | None = None) -> list[dict[str, Any]]:
        return list(self._keys)
