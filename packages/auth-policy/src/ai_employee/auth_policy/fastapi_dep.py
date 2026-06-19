"""FastAPI auth dependencies for cross-service unified auth.

Provides:

* :func:`require_jwt` — a dependency factory that validates a ``Bearer``
  JWT, enforces RBAC permissions, and injects :class:`TokenClaims` into
  the request state.
* :func:`require_internal_or_jwt` — a migration-friendly dependency that
  accepts either a valid JWT **or** the legacy ``X-Internal-Token`` shared
  secret.  Use this to roll out JWT across services without breaking
  existing callers; flip ``JWT_AUTH_STRICT=true`` to drop the legacy
  path once all callers migrate.
* :func:`require_oidc_or_internal` — production-grade dependency that
  prefers an OIDC ``Bearer`` token (RS256) when SSO is enabled, falls
  back to the legacy HS256 JWT or the ``X-Internal-Token`` shared
  secret otherwise.  When ``OIDC_ISSUER`` is unset, the OIDC branch is
  skipped so existing call-sites see no behavioural change.

Both dependencies raise ``401`` on missing/invalid credentials and
``403`` when the principal lacks the required permission.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ai_employee.auth_policy.jwt import (
    JWTError,
    JWTExpired,
    JWTInvalid,
    TokenClaims,
    verify_token,
)
from ai_employee.auth_policy.oidc import (
    OIDCClaims,
    OIDCConfig,
    OIDCDisabled,
    OIDCInvalid,
    build_oidc_verifier,
)
from ai_employee.auth_policy.rbac import can_any
from fastapi import HTTPException, Request, status

_BEARER_PREFIX = "Bearer "


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.startswith(_BEARER_PREFIX):
        return None
    token = authorization[len(_BEARER_PREFIX) :].strip()
    return token or None


def _strict_mode() -> bool:
    return os.getenv("JWT_AUTH_STRICT", "false").strip().lower() == "true"


def _claims_from_request(request: Request) -> TokenClaims | None:
    """Pull and verify a Bearer JWT from the request, or None if absent."""
    auth = request.headers.get("Authorization")
    token = _extract_bearer(auth)
    if token is None:
        return None
    try:
        return verify_token(token)
    except JWTExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "token_expired"},
        )
    except JWTInvalid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "token_invalid"},
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "token_invalid"},
        )


def _internal_token_ok(request: Request, env_var: str = "INTERNAL_TOKEN") -> bool:
    """Validate the legacy X-Internal-Token header against the configured secret.

    ``env_var`` lets services that use a service-specific token
    (e.g. ``KNOWLEDGE_API_INTERNAL_TOKEN``) reuse this check without
    changing the shared ``INTERNAL_TOKEN`` secret.
    """
    expected = os.getenv(env_var)
    if not expected:
        # Fall back to the shared INTERNAL_TOKEN when the service-specific
        # one is unset or empty, so callers configured only with the
        # global secret still authenticate.
        expected = os.getenv("INTERNAL_TOKEN")
        if not expected:
            return False
    provided = request.headers.get("X-Internal-Token")
    return bool(provided) and provided == expected


def _oidc_claims_from_request(
    request: Request,
) -> OIDCClaims | None:
    """Verify the Bearer token as OIDC, or return ``None`` if absent/disabled.

    The OIDC verifier is built once per process and consults
    :class:`OIDCConfig` to decide whether verification is enabled at
    all.  When OIDC is disabled the helper returns ``None`` so the
    caller can fall through to the legacy JWT / internal-token path.
    """
    auth = request.headers.get("Authorization")
    token = _extract_bearer(auth)
    if token is None:
        return None
    verifier = build_oidc_verifier(verify_signature=True)
    if not verifier.config.enabled:
        return None
    try:
        return verifier.verify(token)
    except OIDCDisabled:
        # Should not happen — we just checked ``enabled`` — but be defensive.
        return None
    except OIDCInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "oidc_invalid", "message": str(exc)},
        ) from exc


def require_jwt(
    permissions: list[str] | None = None,
) -> Callable[..., TokenClaims]:
    """Dependency: require a valid JWT carrying one of ``permissions``.

    When ``permissions`` is empty/None, only authentication is enforced.
    """

    def _dep(request: Request) -> TokenClaims:
        claims = _claims_from_request(request)
        if claims is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "authentication_required"},
            )
        if permissions:
            decision = can_any(claims.roles, claims.scopes, permissions)
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "forbidden",
                        "required_permissions": permissions,
                        "missing": decision.missing,
                    },
                )
        request.state.claims = claims
        return claims

    return _dep


def require_internal_or_jwt(
    permissions: list[str] | None = None,
) -> Callable[..., TokenClaims | None]:
    """Migration dependency: accept JWT OR legacy X-Internal-Token.

    Returns the :class:`TokenClaims` when a JWT is presented, or ``None``
    when the legacy internal-token path is used (caller is treated as a
    trusted internal service).  Raises 401 when neither credential is
    valid and 403 on insufficient permission (JWT path only — the
    internal-token path is trusted).
    """

    def _dep(request: Request) -> TokenClaims | None:
        claims = _claims_from_request(request)
        if claims is not None:
            if permissions:
                decision = can_any(claims.roles, claims.scopes, permissions)
                if not decision.allowed:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "error_code": "forbidden",
                            "required_permissions": permissions,
                            "missing": decision.missing,
                        },
                    )
            request.state.claims = claims
            return claims
        # Legacy internal-token path.
        if _strict_mode():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "authentication_required"},
            )
        if _internal_token_ok(request):
            return None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "authentication_required"},
        )

    return _dep


# --------------------------------------------------------------------------- #
# OIDC-aware production dependency (R24-A.3)
# --------------------------------------------------------------------------- #


class OIDCOrInternalPrincipal:
    """Unified principal injected by :func:`require_oidc_or_internal`.

    * ``kind == "oidc"`` — RS256 OIDC token; ``oidc_claims`` populated.
    * ``kind == "jwt"`` — HS256 internal JWT; ``jwt_claims`` populated.
    * ``kind == "internal"`` — legacy ``X-Internal-Token`` accepted;
      all fields are ``None`` and the caller is treated as a trusted
      service.
    """

    __slots__ = ("kind", "oidc_claims", "jwt_claims")

    def __init__(
        self,
        *,
        kind: str,
        oidc_claims: OIDCClaims | None = None,
        jwt_claims: TokenClaims | None = None,
    ) -> None:
        self.kind = kind
        self.oidc_claims = oidc_claims
        self.jwt_claims = jwt_claims

    def subject(self) -> str:
        if self.oidc_claims is not None:
            return self.oidc_claims.sub
        if self.jwt_claims is not None:
            return self.jwt_claims.sub
        return "internal-token-trusted"

    def roles(self) -> list[str]:
        if self.oidc_claims is not None:
            return list(self.oidc_claims.roles)
        if self.jwt_claims is not None:
            return list(self.jwt_claims.roles)
        return []


def _enforce_permissions(principal: OIDCOrInternalPrincipal, perms: list[str]) -> None:
    if principal.oidc_claims is not None:
        # Map OIDC roles → RBAC decision (scopes are empty for OIDC).
        decision = can_any(principal.oidc_claims.roles, [], perms)
    elif principal.jwt_claims is not None:
        decision = can_any(
            principal.jwt_claims.roles, principal.jwt_claims.scopes, perms
        )
    else:
        # Internal token: trusted, no permission check.
        return
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "forbidden",
                "required_permissions": perms,
                "missing": decision.missing,
            },
        )


def require_oidc_or_internal(
    permissions: list[str] | None = None,
    *,
    internal_token_env: str = "INTERNAL_TOKEN",
) -> Callable[..., OIDCOrInternalPrincipal]:
    """Production dependency: OIDC first, HS256 JWT, then internal token.

    Resolution order (mirrors the spec):

    1. If the request carries an ``Authorization: Bearer ...`` header
       **and** OIDC is configured (``OIDC_ISSUER`` set), the token is
       verified as RS256.  Failure raises ``401 oidc_invalid``.
    2. Otherwise (OIDC disabled, or no Bearer header), the legacy
       HS256 JWT path is tried.
    3. If neither yields a valid token, the ``X-Internal-Token`` shared
       secret is checked (rejected in ``JWT_AUTH_STRICT=true`` mode).
       The internal token is read from ``internal_token_env``
       (default ``INTERNAL_TOKEN``); services that historically used a
       service-specific variable (e.g. ``KNOWLEDGE_API_INTERNAL_TOKEN``)
       pass that name here.
    4. If all paths fail, ``401 authentication_required`` is raised.

    The returned :class:`OIDCOrInternalPrincipal` exposes the resolved
    principal's roles / subject so callers can apply endpoint-specific
    authorisation beyond the RBAC permission check.
    """

    def _dep(request: Request) -> OIDCOrInternalPrincipal:
        # Branch 1: OIDC.  Skipped (returns None) when SSO is disabled
        # or when the request has no Authorization header.
        oidc_claims = _oidc_claims_from_request(request)
        if oidc_claims is not None:
            request.state.claims = oidc_claims
            principal = OIDCOrInternalPrincipal(
                kind="oidc", oidc_claims=oidc_claims,
            )
            if permissions:
                _enforce_permissions(principal, permissions)
            return principal
        # Branch 2: legacy HS256 JWT.
        jwt_claims = _claims_from_request(request)
        if jwt_claims is not None:
            request.state.claims = jwt_claims
            principal = OIDCOrInternalPrincipal(
                kind="jwt", jwt_claims=jwt_claims,
            )
            if permissions:
                _enforce_permissions(principal, permissions)
            return principal
        # Branch 3: X-Internal-Token (skipped in strict mode).
        if _strict_mode():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "authentication_required"},
            )
        if _internal_token_ok(request, env_var=internal_token_env):
            return OIDCOrInternalPrincipal(kind="internal")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "authentication_required"},
        )

    return _dep


__all__ = [
    "OIDCOrInternalPrincipal",
    "require_internal_or_jwt",
    "require_jwt",
    "require_oidc_or_internal",
]
