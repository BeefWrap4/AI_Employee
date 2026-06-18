"""auth-policy: JWT (HS256) + RBAC + tool risk-level policy.

Shared authorization primitives for the AI Employee platform.  See
``jwt`` for token issue/verify, ``rbac`` for role/permission
enforcement, and ``policy`` for tool risk-level → permission mapping.
"""

from ai_employee.auth_policy.jwt import (
    DEFAULT_ALGORITHM,
    DEFAULT_AUDIENCE,
    DEFAULT_ISSUER,
    DEFAULT_TTL_SECONDS,
    JWTError,
    JWTExpired,
    JWTInvalid,
    TokenClaims,
    decode_unsafe,
    issue_token,
    verify_token,
)
from ai_employee.auth_policy.policy import (
    ACTION_PERMISSIONS,
    RISK_LEVEL_PERMISSIONS,
    permission_for_action,
    permission_for_risk_level,
)
from ai_employee.auth_policy.rbac import (
    AccessDecision,
    PERM_ADMIN,
    PERM_AGENT_APPROVE,
    PERM_AGENT_RUN,
    PERM_INSPECT,
    PERM_KNOWLEDGE_ADMIN,
    PERM_KNOWLEDGE_READ,
    PERM_KNOWLEDGE_WRITE,
    PERM_RCA_APPROVE,
    PERM_RCA_READ,
    PERM_RCA_WRITE,
    PERM_TOOL_INVOKE,
    PERM_TOOL_REGISTER,
    ROLE_PERMISSIONS,
    can,
    can_access_resource,
    can_any,
    permissions_for,
    permissions_for_roles,
)
from ai_employee.auth_policy.fastapi_dep import (
    require_internal_or_jwt,
    require_jwt,
)

__all__ = [
    "ACTION_PERMISSIONS",
    "AccessDecision",
    "DEFAULT_ALGORITHM",
    "DEFAULT_AUDIENCE",
    "DEFAULT_ISSUER",
    "DEFAULT_TTL_SECONDS",
    "JWTError",
    "JWTExpired",
    "JWTInvalid",
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
    "RISK_LEVEL_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "TokenClaims",
    "can",
    "can_access_resource",
    "can_any",
    "decode_unsafe",
    "issue_token",
    "permission_for_action",
    "permission_for_risk_level",
    "permissions_for",
    "permissions_for_roles",
    "require_internal_or_jwt",
    "require_jwt",
    "verify_token",
]
