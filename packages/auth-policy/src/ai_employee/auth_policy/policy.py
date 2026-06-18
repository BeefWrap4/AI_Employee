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
RISK_LEVEL_PERMISSIONS = {
    "read_only": PERM_TOOL_INVOKE,
    "approval_required": PERM_AGENT_APPROVE,
    "high_risk": PERM_ADMIN,
}

# Action → required permission.
ACTION_PERMISSIONS = {
    "tool.register": PERM_TOOL_REGISTER,
    "tool.invoke": PERM_TOOL_INVOKE,
    "tool.invoke.read_only": PERM_INSPECT,
    "inspect.run": PERM_INSPECT,
}


def permission_for_risk_level(risk_level: str) -> str:
    """Return the permission required to invoke a tool of this risk level."""
    if risk_level not in RISK_LEVEL_PERMISSIONS:
        raise ValueError(f"unknown risk level: {risk_level}")
    return RISK_LEVEL_PERMISSIONS[risk_level]


def permission_for_action(action: str) -> str | None:
    """Return the permission required for a named platform action."""
    return ACTION_PERMISSIONS.get(action)


__all__ = [
    "ACTION_PERMISSIONS",
    "RISK_LEVEL_PERMISSIONS",
    "permission_for_action",
    "permission_for_risk_level",
]
