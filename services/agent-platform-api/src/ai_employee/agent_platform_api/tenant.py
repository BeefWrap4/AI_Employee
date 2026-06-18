"""Tenant context resolution (spec §8.3).

Foundation for multi-tenant isolation.  Today every record is shared
across all callers; this module introduces a :class:`TenantContext`
that downstream code can attach to audit events, runs, and queries.

Resolution order (:func:`resolve_tenant_context`):

1. ``explicit`` query parameter (programmatic override — empty string
   is ignored so callers can pass through without forcing a value).
2. ``X-Tenant-ID`` header.
3. The ``tenant:user`` prefix of the JWT ``sub`` claim (when present).
4. ``public`` (default).

Tenant IDs are validated to be short, alphanumeric, and contain only
``_`` or ``-`` — never whitespace, never unicode, never arbitrary
strings from URL paths.  This prevents log-injection style attacks and
keeps database column widths predictable.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass

_DEFAULT_TENANT = "public"
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ContextVar so runtime / audit calls deep in the call stack can pick
# up the tenant without threading it through every function signature.
_current_tenant: ContextVar[str] = ContextVar(
    "ai_employee_current_tenant", default=_DEFAULT_TENANT,
)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str | None
    source: str  # query | header | subject | default

    def to_dict(self) -> dict:
        return asdict(self)


def get_current_tenant_id() -> str:
    """Return the tenant_id bound to the current request context (default ``public``)."""
    return _current_tenant.get()


def set_current_tenant_id(tenant_id: str) -> object:
    """Bind ``tenant_id`` to the current context and return a token for :func:`reset_current_tenant`."""
    return _current_tenant.set(tenant_id)


def reset_current_tenant(token: object) -> None:
    _current_tenant.reset(token)  # type: ignore[arg-type]


def parse_tenant_from_subject(claims_sub: str | None, *, default: str = _DEFAULT_TENANT) -> str:
    """Extract the tenant prefix from a ``tenant:user`` subject claim.

    Returns ``default`` when the claim is missing or doesn't carry a
    tenant prefix.  Only the first colon-separated segment is consumed
    so user IDs with internal colons are preserved.
    """
    if not claims_sub:
        return default
    if ":" in claims_sub:
        return claims_sub.split(":", 1)[0]
    return default


def _validate_tenant_id(tenant_id: str) -> str:
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(
            f"invalid tenant_id {tenant_id!r}: must be 1-64 chars [A-Za-z0-9_-]",
        )
    return tenant_id


def resolve_tenant_context(
    *,
    explicit: str | None,
    header_tenant: str | None,
    claims_sub: str | None,
) -> TenantContext:
    """Build a :class:`TenantContext` using the documented precedence."""
    if explicit:
        return TenantContext(
            tenant_id=_validate_tenant_id(explicit),
            user_id=claims_sub.split(":", 1)[1] if claims_sub and ":" in claims_sub else None,
            source="query",
        )
    if header_tenant:
        return TenantContext(
            tenant_id=_validate_tenant_id(header_tenant),
            user_id=claims_sub.split(":", 1)[1] if claims_sub and ":" in claims_sub else None,
            source="header",
        )
    tenant = parse_tenant_from_subject(claims_sub)
    user_id = None
    if claims_sub and ":" in claims_sub:
        user_id = claims_sub.split(":", 1)[1]
    if tenant != _DEFAULT_TENANT:
        _validate_tenant_id(tenant)
        return TenantContext(tenant_id=tenant, user_id=user_id, source="subject")
    return TenantContext(tenant_id=_DEFAULT_TENANT, user_id=user_id, source="default")


__all__ = [
    "TenantContext",
    "get_current_tenant_id",
    "parse_tenant_from_subject",
    "reset_current_tenant",
    "resolve_tenant_context",
    "set_current_tenant_id",
]
