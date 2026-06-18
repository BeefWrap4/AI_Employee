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


def _internal_token_ok(request: Request) -> bool:
    """Validate the legacy X-Internal-Token header against the configured secret."""
    expected = os.getenv("INTERNAL_TOKEN")
    if not expected:
        return False
    provided = request.headers.get("X-Internal-Token")
    return bool(provided) and provided == expected


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


__all__ = ["require_internal_or_jwt", "require_jwt"]
