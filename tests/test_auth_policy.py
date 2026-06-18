"""auth-policy tests: JWT issue/verify, RBAC, tool risk policy."""
from __future__ import annotations

import time

import pytest

from ai_employee.auth_policy import (
    JWTError,
    JWTExpired,
    JWTInvalid,
    PERM_AGENT_APPROVE,
    PERM_INSPECT,
    PERM_KNOWLEDGE_READ,
    PERM_KNOWLEDGE_WRITE,
    PERM_RCA_APPROVE,
    PERM_RCA_WRITE,
    PERM_TOOL_INVOKE,
    PERM_TOOL_REGISTER,
    TokenClaims,
    can,
    can_access_resource,
    can_any,
    issue_token,
    permission_for_action,
    permission_for_risk_level,
    permissions_for,
    permissions_for_roles,
    verify_token,
)


SECRET = "test-secret-please-rotate"


# --- JWT ----------------------------------------------------------------- #


def test_issue_and_verify_roundtrip() -> None:
    token = issue_token(
        subject="svc:knowledge-api",
        roles=["operator"],
        scopes=["knowledge:write"],
        secret=SECRET,
    )
    claims = verify_token(token, secret=SECRET)
    assert claims.sub == "svc:knowledge-api"
    assert "operator" in claims.roles
    assert "knowledge:write" in claims.scopes
    assert claims.iss == "ai-employee"
    assert claims.aud == "ai-employee-services"
    assert claims.exp > claims.iat


def test_verify_rejects_wrong_secret() -> None:
    token = issue_token(subject="u1", secret=SECRET)
    with pytest.raises(JWTInvalid):
        verify_token(token, secret="wrong-secret")


def test_verify_rejects_expired_token() -> None:
    token = issue_token(subject="u1", secret=SECRET, ttl_seconds=-10)
    with pytest.raises(JWTExpired):
        verify_token(token, secret=SECRET)


def test_verify_rejects_wrong_audience() -> None:
    token = issue_token(subject="u1", secret=SECRET, audience="other")
    with pytest.raises(JWTInvalid):
        verify_token(token, secret=SECRET, audience="ai-employee-services")


def test_verify_rejects_wrong_issuer() -> None:
    token = issue_token(subject="u1", secret=SECRET, issuer="evil")
    with pytest.raises(JWTInvalid):
        verify_token(token, secret=SECRET, issuer="ai-employee")


def test_issue_requires_secret(monkeypatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(JWTError):
        issue_token(subject="u1")


def test_issue_requires_subject() -> None:
    with pytest.raises(JWTError):
        issue_token(subject="", secret=SECRET)


def test_token_claims_helpers() -> None:
    claims = TokenClaims(
        sub="u1", roles=["operator"], scopes=["knowledge:read"],
        exp=0, iat=0, iss="x", aud="y", raw={},
    )
    assert claims.has_role("operator")
    assert not claims.has_role("admin")
    assert claims.has_scope("knowledge:read")
    assert not claims.has_any_scope(["rca:approve"])
    # wildcard
    wild = TokenClaims(
        sub="u2", roles=[], scopes=["*"],
        exp=0, iat=0, iss="x", aud="y", raw={},
    )
    assert wild.has_scope("anything")
    assert wild.has_any_scope(["a", "b", "c"])


# --- RBAC ---------------------------------------------------------------- #


def test_permissions_for_roles_flattens() -> None:
    perms = permissions_for_roles(["viewer"])
    assert PERM_KNOWLEDGE_READ in perms
    assert PERM_KNOWLEDGE_WRITE not in perms


def test_permissions_combines_roles_and_scopes() -> None:
    perms = permissions_for(["viewer"], ["rca:read"])
    assert PERM_KNOWLEDGE_READ in perms
    assert "rca:read" in perms


def test_admin_wildcard_grants_everything() -> None:
    perms = permissions_for(["admin"], [])
    assert "*" in perms
    assert can(["admin"], [], PERM_TOOL_REGISTER).allowed


def test_can_grants_when_permission_present() -> None:
    decision = can(["operator"], [], PERM_KNOWLEDGE_WRITE)
    assert decision.allowed


def test_can_denies_when_permission_missing() -> None:
    decision = can(["viewer"], [], PERM_KNOWLEDGE_WRITE)
    assert not decision.allowed
    assert PERM_KNOWLEDGE_WRITE in decision.missing


def test_can_any_satisfied_by_one() -> None:
    decision = can_any(["reviewer"], [], [PERM_KNOWLEDGE_WRITE, PERM_RCA_APPROVE])
    assert decision.allowed


def test_can_access_resource_allows_owner() -> None:
    decision = can_access_resource(
        ["viewer"], [], PERM_KNOWLEDGE_WRITE,
        resource_owner="alice", subject="alice",
    )
    assert decision.allowed
    assert "owner" in decision.reason


def test_can_access_resource_denies_non_owner() -> None:
    decision = can_access_resource(
        ["viewer"], [], PERM_KNOWLEDGE_WRITE,
        resource_owner="alice", subject="bob",
    )
    assert not decision.allowed


# --- Tool risk policy ---------------------------------------------------- #


def test_risk_level_permissions() -> None:
    assert permission_for_risk_level("read_only") == PERM_TOOL_INVOKE
    assert permission_for_risk_level("approval_required") == PERM_AGENT_APPROVE
    assert permission_for_risk_level("high_risk") == "*"


def test_unknown_risk_level_raises() -> None:
    with pytest.raises(ValueError):
        permission_for_risk_level("unknown")


def test_action_permissions() -> None:
    assert permission_for_action("tool.register") == PERM_TOOL_REGISTER
    assert permission_for_action("inspect.run") == PERM_INSPECT
    assert permission_for_action("unknown") is None


def test_operator_cannot_register_tools() -> None:
    decision = can(["operator"], [], PERM_TOOL_REGISTER)
    assert not decision.allowed


def test_admin_can_register_and_invoke() -> None:
    assert can(["admin"], [], PERM_TOOL_REGISTER).allowed
    assert can(["admin"], [], PERM_TOOL_INVOKE).allowed
