"""auth-policy: JWT (HS256) + RBAC + tool risk-level policy.

Shared authorization primitives for the AI Employee platform.  See
``jwt`` for token issue/verify, ``rbac`` for role/permission
enforcement, and ``policy`` for tool risk-level → permission mapping.
"""

from ai_employee.auth_policy.casbin_engine import (  # noqa: F401
    build_casbin_engine,
    casbin_check,
)
from ai_employee.auth_policy.fastapi_dep import (
    require_internal_or_jwt,
    require_jwt,
)
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
from ai_employee.auth_policy.oidc import (
    OIDCClaims,
    OIDCConfig,
    OIDCDisabled,
    OIDCInvalid,
    OIDCVerifier,
    build_oidc_verifier,
    verify_oidc_token,
)
from ai_employee.auth_policy.policy import (
    ACTION_PERMISSIONS,
    RISK_LEVEL_PERMISSIONS,
    normalise_risk_level,
    permission_for_action,
    permission_for_risk_level,
)
from ai_employee.auth_policy.rbac import (
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
    AccessDecision,
    can,
    can_access_resource,
    can_any,
    permissions_for,
    permissions_for_roles,
)

__all__ = [
    "ACTION_PERMISSIONS",
    "DEFAULT_ALGORITHM",
    "DEFAULT_AUDIENCE",
    "DEFAULT_ISSUER",
    "DEFAULT_TTL_SECONDS",
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
    "AccessDecision",
    "JWTError",
    "JWTExpired",
    "JWTInvalid",
    "OIDCClaims",
    "OIDCConfig",
    "OIDCDisabled",
    "OIDCInvalid",
    "OIDCVerifier",
    "TokenClaims",
    "build_oidc_verifier",
    "can",
    "can_access_resource",
    "can_any",
    "decode_unsafe",
    "issue_token",
    "normalise_risk_level",
    "permission_for_action",
    "permission_for_risk_level",
    "permissions_for",
    "permissions_for_roles",
    "require_internal_or_jwt",
    "require_jwt",
    "verify_oidc_token",
    "verify_token",
]
