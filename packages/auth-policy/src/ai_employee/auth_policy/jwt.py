"""JWT (HS256) issue / verify helpers built on PyJWT.

Cross-service unified auth: a service issues a short-lived JWT signed
with a shared ``JWT_SECRET`` and downstream services verify it to
authenticate inter-service calls.  Claims follow the standard
``sub`` / ``roles`` / ``scopes`` / ``exp`` / ``iat`` / ``aud`` / ``iss``
shape.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import jwt as _pyjwt
from jwt import ExpiredSignatureError, InvalidTokenError

DEFAULT_ALGORITHM = "HS256"
DEFAULT_ISSUER = "ai-employee"
DEFAULT_AUDIENCE = "ai-employee-services"
DEFAULT_TTL_SECONDS = 3600


class JWTError(Exception):
    """Base error for all JWT failures."""


class JWTInvalid(JWTError):
    """Token signature / claims invalid (401)."""


class JWTExpired(JWTError):
    """Token has expired (401)."""


@dataclass
class TokenClaims:
    sub: str
    roles: list[str]
    scopes: list[str]
    exp: int
    iat: int
    iss: str
    aud: str
    raw: dict[str, Any]

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_scope(self, scope: str) -> bool:
        # Wildcard scope grants everything.
        return "*" in self.scopes or scope in self.scopes

    def has_any_scope(self, scopes: list[str]) -> bool:
        if not scopes:
            return True
        return any(self.has_scope(s) for s in scopes)


def issue_token(
    *,
    subject: str,
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    secret: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    issuer: str = DEFAULT_ISSUER,
    audience: str = DEFAULT_AUDIENCE,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a signed HS256 JWT for inter-service auth."""
    if not subject:
        raise JWTError("subject is required")
    key = secret or os.getenv("JWT_SECRET")
    if not key:
        raise JWTError("JWT_SECRET is not configured")
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "roles": list(roles or []),
        "scopes": list(scopes or []),
        "iat": now,
        "exp": now + int(ttl_seconds),
        "iss": issuer,
        "aud": audience,
    }
    if extra_claims:
        payload.update(extra_claims)
    return _pyjwt.encode(payload, key, algorithm=DEFAULT_ALGORITHM)


def verify_token(
    token: str,
    *,
    secret: str | None = None,
    issuer: str | None = DEFAULT_ISSUER,
    audience: str | None = DEFAULT_AUDIENCE,
    leeway_seconds: int = 0,
) -> TokenClaims:
    """Verify a JWT signature + claims and return decoded claims.

    Raises :class:`JWTExpired` on exp, :class:`JWTInvalid` otherwise.
    """
    key = secret or os.getenv("JWT_SECRET")
    if not key:
        raise JWTError("JWT_SECRET is not configured")
    options: dict[str, Any] = {"require": ["exp", "iat", "sub"]}
    try:
        decoded = _pyjwt.decode(
            token,
            key,
            algorithms=[DEFAULT_ALGORITHM],
            issuer=issuer,
            audience=audience,
            leeway=leeway_seconds,
            options=options,
        )
    except ExpiredSignatureError as exc:
        raise JWTExpired(str(exc)) from exc
    except InvalidTokenError as exc:
        raise JWTInvalid(str(exc)) from exc
    return TokenClaims(
        sub=str(decoded.get("sub", "")),
        roles=list(decoded.get("roles", []) or []),
        scopes=list(decoded.get("scopes", []) or []),
        exp=int(decoded.get("exp", 0)),
        iat=int(decoded.get("iat", 0)),
        iss=str(decoded.get("iss", "")),
        aud=str(decoded.get("aud", "")),
        raw=decoded,
    )


def decode_unsafe(token: str) -> dict[str, Any]:
    """Decode a token without verifying the signature (introspection only)."""
    return _pyjwt.decode(token, options={"verify_signature": False})


__all__ = [
    "DEFAULT_ALGORITHM",
    "DEFAULT_AUDIENCE",
    "DEFAULT_ISSUER",
    "DEFAULT_TTL_SECONDS",
    "JWTError",
    "JWTExpired",
    "JWTInvalid",
    "TokenClaims",
    "decode_unsafe",
    "issue_token",
    "verify_token",
]
