"""Role-Based Access Control policy definitions.

Maps roles → permission sets and enforces access decisions for both
service-level scopes and resource-level permissions.  Roles are
intentionally coarse-grained; fine-grained access is expressed via
scopes carried in the JWT.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical permission strings.  Naming: ``<domain>:<action>``.
PERM_KNOWLEDGE_READ = "knowledge:read"
PERM_KNOWLEDGE_WRITE = "knowledge:write"
PERM_KNOWLEDGE_ADMIN = "knowledge:admin"
PERM_RCA_READ = "rca:read"
PERM_RCA_WRITE = "rca:write"
PERM_RCA_APPROVE = "rca:approve"
PERM_AGENT_RUN = "agent:run"
PERM_AGENT_APPROVE = "agent:approve"
PERM_TOOL_REGISTER = "tool:register"
PERM_TOOL_INVOKE = "tool:invoke"
PERM_INSPECT = "inspect:run"
PERM_ADMIN = "*"


# Role → permission set.  Admin implicitly carries the wildcard.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {PERM_KNOWLEDGE_READ, PERM_RCA_READ},
    "operator": {
        PERM_KNOWLEDGE_READ,
        PERM_KNOWLEDGE_WRITE,
        PERM_RCA_READ,
        PERM_RCA_WRITE,
        PERM_AGENT_RUN,
        PERM_TOOL_INVOKE,
        PERM_INSPECT,
    },
    "reviewer": {
        PERM_KNOWLEDGE_READ,
        PERM_RCA_READ,
        PERM_RCA_WRITE,
        PERM_RCA_APPROVE,
        PERM_AGENT_APPROVE,
    },
    "admin": {PERM_ADMIN},
}


@dataclass
class AccessDecision:
    allowed: bool
    reason: str = ""
    missing: list[str] = field(default_factory=list)


def permissions_for_roles(roles: list[str]) -> set[str]:
    """Flatten the permission set granted by a list of roles."""
    granted: set[str] = set()
    for role in roles:
        granted.update(ROLE_PERMISSIONS.get(role, set()))
    return granted


def permissions_for(roles: list[str], scopes: list[str]) -> set[str]:
    """Combine role-derived permissions with explicit JWT scopes.

    Scopes are treated as additional permissions; the wildcard scope
    ``*`` (granted to admin) opens every permission.
    """
    if PERM_ADMIN in scopes or "*" in scopes:
        return {PERM_ADMIN}
    granted = permissions_for_roles(roles)
    granted.update(scopes)
    return granted


def can(roles: list[str], scopes: list[str], permission: str) -> AccessDecision:
    """Decide whether a principal (roles + scopes) holds ``permission``."""
    granted = permissions_for(roles, scopes)
    if PERM_ADMIN in granted or "*" in granted:
        return AccessDecision(allowed=True, reason="admin wildcard")
    if permission in granted:
        return AccessDecision(allowed=True, reason=f"granted: {permission}")
    return AccessDecision(
        allowed=False,
        reason=f"missing permission: {permission}",
        missing=[permission],
    )


def can_any(roles: list[str], scopes: list[str], permissions: list[str]) -> AccessDecision:
    """Decide whether a principal holds *any* of the given permissions."""
    if not permissions:
        return AccessDecision(allowed=True, reason="no permission required")
    granted = permissions_for(roles, scopes)
    if PERM_ADMIN in granted or "*" in granted:
        return AccessDecision(allowed=True, reason="admin wildcard")
    for perm in permissions:
        if perm in granted:
            return AccessDecision(allowed=True, reason=f"granted: {perm}")
    return AccessDecision(
        allowed=False,
        reason=f"missing any of: {', '.join(permissions)}",
        missing=list(permissions),
    )


def can_access_resource(
    roles: list[str],
    scopes: list[str],
    permission: str,
    *,
    resource_owner: str | None = None,
    subject: str | None = None,
) -> AccessDecision:
    """Resource-level access: permission OR resource ownership."""
    decision = can(roles, scopes, permission)
    if decision.allowed:
        return decision
    if resource_owner and subject and resource_owner == subject:
        return AccessDecision(allowed=True, reason="resource owner")
    return decision


__all__ = [
    "AccessDecision",
    "PERM_ADMIN",
    "PERM_AGENT_APPROVE",
    "PERM_AGENT_RUN",
    "PERM_INSPECT",
    "PERM_KNOWLEDGE_ADMIN",
    "PERM_KNOWLEDGE_READ",
    "PERM_KNOWLEDGE_WRITE",
    "PERM_RCA_APPROVE",
    "PERM_RCA_READ",
    "PERM_RCA_WRITE",
    "PERM_TOOL_INVOKE",
    "PERM_TOOL_REGISTER",
    "ROLE_PERMISSIONS",
    "can",
    "can_access_resource",
    "can_any",
    "permissions_for",
    "permissions_for_roles",
]
