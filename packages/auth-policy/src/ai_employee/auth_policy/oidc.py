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
    def from_env(cls) -> OIDCConfig:
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
    """Fetches the JWKS over HTTP and caches with TTL.

    Production behaviour:

    * First call to :meth:`fetch` performs an HTTP GET and caches the
      parsed ``keys`` list alongside the timestamp of the fetch.
    * Subsequent calls within ``jwks_ttl_s`` return the cached keys.
    * If the caller requests a specific ``kid`` and it is not in the
      cached set, the cache is invalidated and the JWKS is re-fetched
      once (handles key rotation without waiting for TTL expiry).
    * When the TTL has elapsed, the cache is refreshed on the next
      call (with refresh-on-kid-miss still applicable).
    """

    def __init__(
        self,
        url: str,
        *,
        http_client: Any = None,
        timeout_s: float = 2.0,
        jwks_ttl_s: float = 3600.0,
        clock: Any = None,
    ) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._http = http_client
        self._jwks_ttl_s = float(jwks_ttl_s)
        self._clock = clock or time.time
        self._cache: list[dict[str, Any]] | None = None
        self._fetched_at: float | None = None

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        return httpx.Client(timeout=self._timeout_s)

    def _http_get(self) -> list[dict[str, Any]]:
        client = self._client()
        resp = client.get(self._url, timeout=self._timeout_s)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        body = resp.json()
        keys = body.get("keys", []) if isinstance(body, dict) else []
        return list(keys)

    def _cache_expired(self) -> bool:
        if self._cache is None or self._fetched_at is None:
            return True
        return (self._clock() - self._fetched_at) >= self._jwks_ttl_s

    def _store(self, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._cache = list(keys)
        self._fetched_at = self._clock()
        return list(self._cache)

    def fetch(self, kid: str | None = None) -> list[dict[str, Any]]:
        """Return JWKS keys, refreshing on cache miss or TTL expiry.

        If ``kid`` is given and the cached JWKS does not contain it, the
        cache is invalidated and the JWKS is re-fetched once.  The
        re-fetched keys are returned regardless of whether the requested
        ``kid`` is present (the caller raises ``OIDCInvalid`` if the key
        is still missing).
        """
        if self._cache is not None and not self._cache_expired():
            if kid is None or any(k.get("kid") == kid for k in self._cache):
                return list(self._cache)
        # Cold cache, expired TTL, or kid miss → fetch.
        return self._store(self._http_get())


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

    Uses :func:`pyjwt.decode` with ``verify_signature=True`` against
    each candidate JWKS key (filtered by ``kid`` when present) so a
    tampered token or one signed with a different key is rejected.  The
    issuer / audience / expiry checks are performed separately by
    :func:`verify_oidc_token` after this returns.
    """
    try:
        import jwt as pyjwt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OIDCInvalid(
            "PyJWT is required for OIDC signature verification",
        ) from exc
    kid = header.get("kid")
    candidate_keys = [k for k in jwks if (not kid or k.get("kid") == kid)]
    if not candidate_keys:
        raise OIDCInvalid(f"no matching JWKS key for kid={kid!r}")
    alg = header.get("alg") or "RS256"
    if alg not in {"RS256", "RS384", "RS512"}:
        raise OIDCInvalid(f"unsupported alg={alg!r}")
    last_err: Exception | None = None
    for key in candidate_keys:
        try:
            public_key = pyjwt.PyJWK(key).key
        except Exception as exc:
            last_err = exc
            continue
        try:
            pyjwt.decode(
                token,
                key=public_key,
                algorithms=[alg],
                # Only verify the signature here.  Claim checks (iss/aud/exp)
                # are performed by :func:`verify_oidc_token` after this
                # returns so a single failing claim doesn't mask a real
                # signature mismatch.
                options={
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            return
        except pyjwt.InvalidSignatureError as exc:
            last_err = exc
            continue
        except pyjwt.InvalidTokenError as exc:
            last_err = exc
            continue
    raise OIDCInvalid(
        f"signature verification failed: {last_err!r}" if last_err
        else "signature verification failed: no usable key"
    )


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
        # Pass the kid so refresh-on-kid-miss can fire when the cache
        # doesn't contain it.
        keys = jwks_client.fetch(kid=header.get("kid"))
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
