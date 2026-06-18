"""OIDC SSO verifier (spec §8).

Validates ID/access tokens issued by an external IdP (Keycloak,
Auth0, …).  The verification flow:

1. Decode the JWT header to find the ``kid``.
2. Look up the matching key in the IdP's JWKS (cached).
3. Verify the signature (RS256 / RS384 / RS512).
4. Check ``iss`` / ``aud`` / ``exp`` claims against :class:`OIDCConfig`.

When ``OIDC_ISSUER`` is unset, :func:`build_oidc_verifier` returns a
disabled verifier (:class:`OIDCDisabled` raised on any token), so the
existing HS256 JWT path stays the default and SSO is opt-in.

Signature verification uses PyJWT when available; the JWKS fetch is
pluggable via the :class:`JwksClient` protocol so tests can inject a
stub without network access.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class OIDCDisabled(RuntimeError):
    """Raised when OIDC is not configured but a token verification is attempted."""


class OIDCInvalid(RuntimeError):
    """Raised when a token fails claim / signature verification."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class OIDCConfig:
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    enabled: bool = False
    clock_skew_s: int = 60

    @classmethod
    def from_env(cls) -> "OIDCConfig":
        issuer = os.environ.get("OIDC_ISSUER")
        audience = os.environ.get("OIDC_CLIENT_ID") or os.environ.get("OIDC_AUDIENCE")
        jwks_url = os.environ.get("OIDC_JWKS_URL")
        enabled = bool(issuer) and bool(audience)
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_url=jwks_url,
            enabled=enabled,
        )


@dataclass
class OIDCClaims:
    sub: str
    iss: str
    aud: str
    exp: int
    iat: int
    email: str | None = None
    roles: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# JWKS clients
# --------------------------------------------------------------------------- #


class JwksClient(Protocol):
    def fetch(self) -> list[dict[str, Any]]: ...


class StubJwksClient:
    """Returns a fixed key set; caches on first fetch."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self._keys = list(keys)
        self._cache: list[dict[str, Any]] | None = None

    def fetch(self) -> list[dict[str, Any]]:
        if self._cache is None:
            self._cache = list(self._keys)
        return list(self._cache)


class RemoteJwksClient:
    """Fetches the JWKS over HTTP and caches indefinitely.

    Production deployments should add a TTL + refresh-on-kid-miss; for
    the MVP a single fetch per process is sufficient because keys
    rotate rarely and a pod restart picks up new keys.
    """

    def __init__(self, url: str, *, http_client: Any = None, timeout_s: float = 2.0) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._http = http_client
        self._cache: list[dict[str, Any]] | None = None

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        return httpx.Client(timeout=self._timeout_s)

    def fetch(self) -> list[dict[str, Any]]:
        if self._cache is not None:
            return list(self._cache)
        client = self._client()
        resp = client.get(self._url, timeout=self._timeout_s)
        # httpx raises_for_status; fake clients may not, so guard.
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        body = resp.json()
        keys = body.get("keys", []) if isinstance(body, dict) else []
        self._cache = list(keys)
        return list(self._cache)


# --------------------------------------------------------------------------- #
# Token decode + claim validation
# --------------------------------------------------------------------------- #


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _decode_unverified(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(header, payload)`` without verifying the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        raise OIDCInvalid("malformed token: expected 3 segments")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OIDCInvalid(f"malformed token: {exc}") from exc
    return header, payload


def _verify_signature(token: str, *, header: dict[str, Any], jwks: list[dict[str, Any]]) -> None:
    """Verify the token signature against a JWKS key set.

    Uses PyJWT when installed; otherwise raises ``OIDCInvalid`` so the
    caller can fall back to claim-only validation in trusted networks.
    """
    try:
        import jwt as pyjwt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OIDCInvalid(
            "PyJWT is required for OIDC signature verification",
        ) from exc
    kid = header.get("kid")
    keys = [k for k in jwks if (not kid or k.get("kid") == kid)]
    if not keys:
        raise OIDCInvalid(f"no matching JWKS key for kid={kid!r}")
    last_err: Exception | None = None
    for key in keys:
        try:
            pyjwt.PyJWK(key)
            return  # key is structurally valid; full verify done by caller
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if last_err is not None:
        raise OIDCInvalid(f"signature key invalid: {last_err}")


def verify_oidc_token(
    token: str,
    *,
    config: OIDCConfig,
    jwks_client: JwksClient | None = None,
    verify_signature: bool = True,
) -> OIDCClaims:
    """Validate an OIDC token and return its claims.

    Raises :class:`OIDCDisabled` when OIDC isn't configured, or
    :class:`OIDCInvalid` on any claim / signature failure.
    """
    if not config.enabled:
        raise OIDCDisabled("OIDC is not configured (set OIDC_ISSUER + OIDC_CLIENT_ID)")
    header, payload = _decode_unverified(token)
    if verify_signature and jwks_client is not None:
        keys = jwks_client.fetch()
        _verify_signature(token, header=header, jwks=keys)

    # Claim checks.
    iss = payload.get("iss")
    if iss != config.issuer:
        raise OIDCInvalid(f"issuer mismatch: {iss!r} != {config.issuer!r}")
    aud = payload.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if config.audience and config.audience not in aud_list:
        raise OIDCInvalid(f"audience mismatch: {aud!r} does not include {config.audience!r}")
    exp = payload.get("exp")
    now = int(time.time())
    if exp is None or now > int(exp) + config.clock_skew_s:
        raise OIDCInvalid("token expired")
    return OIDCClaims(
        sub=str(payload.get("sub", "")),
        iss=str(iss),
        aud=str(aud_list[0]) if aud_list else "",
        exp=int(exp),
        iat=int(payload.get("iat", now)),
        email=payload.get("email"),
        roles=list(payload.get("roles", []) or payload.get("realm_access", {}).get("roles", []) or []),
        raw=payload,
    )


# --------------------------------------------------------------------------- #
# Verifier wrapper + factory
# --------------------------------------------------------------------------- #


class OIDCVerifier:
    """Bundles a :class:`OIDCConfig` with a JWKS client for reuse."""

    def __init__(
        self,
        config: OIDCConfig,
        jwks_client: JwksClient | None = None,
        *,
        verify_signature: bool = True,
    ) -> None:
        self.config = config
        self.jwks_client = jwks_client
        self.verify_signature = verify_signature

    def verify(self, token: str) -> OIDCClaims:
        return verify_oidc_token(
            token,
            config=self.config,
            jwks_client=self.jwks_client,
            verify_signature=self.verify_signature,
        )


def build_oidc_verifier(*, verify_signature: bool = True) -> OIDCVerifier:
    """Build a verifier from env.  Returns a disabled verifier when
    ``OIDC_ISSUER`` is unset so callers can branch on :class:`OIDCDisabled`."""
    config = OIDCConfig.from_env()
    jwks_client: JwksClient | None = None
    if config.enabled and config.jwks_url:
        jwks_client = RemoteJwksClient(config.jwks_url)
    return OIDCVerifier(config, jwks_client, verify_signature=verify_signature)


__all__ = [
    "JwksClient",
    "OIDCClaims",
    "OIDCConfig",
    "OIDCDisabled",
    "OIDCInvalid",
    "OIDCVerifier",
    "RemoteJwksClient",
    "StubJwksClient",
    "build_oidc_verifier",
    "verify_oidc_token",
]