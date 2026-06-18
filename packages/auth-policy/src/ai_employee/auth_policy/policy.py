"""Tool risk-level → required permission policy.

Maps the platform's tool risk levels (``read_only``,
``approval_required``, ``high_risk``) to the permission a principal
must hold before invoking the tool.  Keeps the policy in one place so
the agent platform and tool-registry service agree on enforcement.
"""

from __future__ import annotations

from ai_employee.auth_policy.rbac import (
    PERM_ADMIN,
    PERM_AGENT_APPROVE,
    PERM_INSPECT,
    PERM_TOOL_INVOKE,
    PERM_TOOL_REGISTER,
)

# Risk level → required permission to invoke.
# Spec §5.3 canonical 4-tier risk levels.
RISK_LEVEL_PERMISSIONS = {
    "readonly": PERM_TOOL_INVOKE,
    "suggest": PERM_TOOL_INVOKE,
    "approval_required": PERM_AGENT_APPROVE,
    # Forbidden tools cannot be invoked by anyone through this service
    # (enforced at the tool-registry endpoint, not in this mapping).
    "forbidden": PERM_ADMIN,
}
# Legacy aliases — mapped to the canonical level they were renamed from.
RISK_LEVEL_ALIASES = {
    "read_only": "readonly",
    "high_risk": "approval_required",
}

# Action → required permission.
ACTION_PERMISSIONS = {
    "tool.register": PERM_TOOL_REGISTER,
    "tool.invoke": PERM_TOOL_INVOKE,
    "tool.invoke.read_only": PERM_INSPECT,
    "inspect.run": PERM_INSPECT,
}


def normalise_risk_level(risk_level: str) -> str:
    """Return the canonical risk level, mapping legacy aliases."""
    if risk_level in RISK_LEVEL_PERMISSIONS:
        return risk_level
    if risk_level in RISK_LEVEL_ALIASES:
        return RISK_LEVEL_ALIASES[risk_level]
    raise ValueError(f"unknown risk level: {risk_level}")


def permission_for_risk_level(risk_level: str) -> str:
    """Return the permission required to invoke a tool of this risk level."""
    return RISK_LEVEL_PERMISSIONS[normalise_risk_level(risk_level)]


def permission_for_action(action: str) -> str | None:
    """Return the permission required for a named platform action."""
    return ACTION_PERMISSIONS.get(action)


__all__ = [
    "ACTION_PERMISSIONS",
    "RISK_LEVEL_PERMISSIONS",
    "permission_for_action",
    "permission_for_risk_level",
]
