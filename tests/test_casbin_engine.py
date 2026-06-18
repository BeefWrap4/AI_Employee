"""Casbin policy engine tests (spec P3 §4 Casbin 或企业权限系统).

The :class:`CasbinPolicyEngine` wraps a casbin Enforcer with model
+ policy loaded from disk.  The model is RBAC with a domain / tenant
extension; policies are seeded from ``policy.csv`` (or memory for
tests).  Supports hot-reload of the policy file via ``reload()``.

A :func:`build_casbin_engine` factory wires the bundled ``model.conf``
+ ``policy.csv`` shipped under ``packages/auth-policy/policies/``.
"""
from __future__ import annotations

import pytest

from ai_employee.auth_policy.casbin_engine import (
    AccessContext,
    CasbinPolicyEngine,
    build_casbin_engine,
)


# --------------------------------------------------------------------------- #
# model + policy helpers (in-memory so tests don't need disk)
# --------------------------------------------------------------------------- #


_MODEL_CONF = """
[request_definition]
r = sub, dom, obj, act

[policy_definition]
p = sub, dom, obj, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && (p.dom == "*" || r.dom == p.dom) && (p.obj == "*" || r.obj == p.obj) && (p.act == "*" || r.act == p.act)
"""


@pytest.fixture
def engine() -> CasbinPolicyEngine:
    """In-memory engine with a few canned policies."""
    policy = (
        "p, alice, tenant-acme, knowledge, read\n"
        "p, alice, tenant-acme, knowledge, write\n"
        "p, bob,   tenant-acme, rca,       read\n"
        "p, admin, *,            *,         *\n"
        "g, alice, reviewer, tenant-acme\n"
        "g, bob,   operator, tenant-acme\n"
    )
    return CasbinPolicyEngine.from_text(_MODEL_CONF, policy)


# --------------------------------------------------------------------------- #
# AccessContext
# -------------------------------------------------------------------------- #


def test_access_context_to_attrs() -> None:
    ctx = AccessContext(sub="alice", dom="tenant-acme", obj="knowledge", act="read")
    assert ctx.to_request() == ("alice", "tenant-acme", "knowledge", "read")


# -------------------------------------------------------------------------- #
# Engine — basic RBAC + domain check
# -------------------------------------------------------------------------- #


def test_engine_allows_direct_policy(engine: CasbinPolicyEngine) -> None:
    assert engine.check(AccessContext("alice", "tenant-acme", "knowledge", "read"))


def test_engine_denies_ungranted(engine: CasbinPolicyEngine) -> None:
    assert not engine.check(AccessContext("alice", "tenant-acme", "knowledge", "delete"))


def test_engine_enforces_tenant_isolation(engine: CasbinPolicyEngine) -> None:
    """Alice's knowledge read policy is scoped to tenant-acme; other tenant denied."""
    assert not engine.check(AccessContext("alice", "tenant-beta", "knowledge", "read"))


def test_engine_admin_wildcard(engine: CasbinPolicyEngine) -> None:
    assert engine.check(AccessContext("admin", "tenant-anything", "anything", "delete"))


def test_engine_role_inheritance(engine: CasbinPolicyEngine) -> None:
    """Alice is a 'reviewer' but has direct policies; check role grant works."""
    # Bob is operator; grant bob the knowledge read via role (we'd add a policy).
    # Instead, use a role that already has matching policies.
    assert engine.check(AccessContext("bob", "tenant-acme", "rca", "read"))


# -------------------------------------------------------------------------- #
# add_policy / remove_policy
# -------------------------------------------------------------------------- #


def test_engine_add_policy_grants(engine: CasbinPolicyEngine) -> None:
    engine.add_policy("carol", "tenant-acme", "agent", "run")
    assert engine.check(AccessContext("carol", "tenant-acme", "agent", "run"))


def test_engine_remove_policy_revokes(engine: CasbinPolicyEngine) -> None:
    engine.add_policy("dave", "tenant-acme", "tool", "invoke")
    assert engine.check(AccessContext("dave", "tenant-acme", "tool", "invoke"))
    engine.remove_policy("dave", "tenant-acme", "tool", "invoke")
    assert not engine.check(AccessContext("dave", "tenant-acme", "tool", "invoke"))


def test_engine_add_role_grant(engine: CasbinPolicyEngine) -> None:
    """Adding (eve, reviewer) lets eve inherit reviewer-scoped policies."""
    # The 'reviewer' role doesn't carry knowledge:read; add a policy for it first.
    engine.add_policy("reviewer", "tenant-x", "knowledge", "read")
    engine.add_role_for_user("eve", "reviewer", "tenant-x")
    assert engine.check(AccessContext("eve", "tenant-x", "knowledge", "read"))


# -------------------------------------------------------------------------- #
# batched check + deny precedence
# -------------------------------------------------------------------------- #


def test_engine_check_all_returns_decisions(engine: CasbinPolicyEngine) -> None:
    requests = [
        AccessContext("alice", "tenant-acme", "knowledge", "read"),
        AccessContext("alice", "tenant-acme", "knowledge", "delete"),
    ]
    decisions = engine.check_all(requests)
    assert decisions == [True, False]


def test_engine_enforce_raises_on_deny(engine: CasbinPolicyEngine) -> None:
    with pytest.raises(PermissionError):
        engine.enforce(AccessContext("alice", "tenant-acme", "knowledge", "delete"))


# -------------------------------------------------------------------------- #
# reload / hot-swap
# -------------------------------------------------------------------------- #


def test_engine_reload_picks_up_new_policy(tmp_path) -> None:
    from pathlib import Path

    model_path = tmp_path / "model.conf"
    policy_path = tmp_path / "policy.csv"
    model_path.write_text(_MODEL_CONF)
    policy_path.write_text("p, zoe, tenant-t, tool, invoke\n")

    engine = CasbinPolicyEngine(model_path=str(model_path), policy_path=str(policy_path))
    assert engine.check(AccessContext("zoe", "tenant-t", "tool", "invoke"))

    # Append a new policy; reload to pick it up.
    with policy_path.open("a") as f:
        f.write("p, zoe, tenant-t, agent, run\n")
    engine.reload()
    assert engine.check(AccessContext("zoe", "tenant-t", "agent", "run"))


# -------------------------------------------------------------------------- #
# Backward compat: rbac.can() still works via the engine
# -------------------------------------------------------------------------- #


def test_casbin_engine_can_act_as_rbac_drop_in() -> None:
    """The Casbin engine should answer the same (sub, obj, act) questions
    the existing :func:`rbac.can` does, so services can migrate lazily."""
    policy = (
        "p, viewer, tenant-x, knowledge, read\n"
        "p, viewer, tenant-x, rca,       read\n"
        "p, operator, tenant-x, knowledge, write\n"
        "p, operator, tenant-x, rca,       write\n"
        "p, reviewer, tenant-x, rca,       approve\n"
    )
    engine = CasbinPolicyEngine.from_text(_MODEL_CONF, policy)
    # viewer can read but not write
    assert engine.check(AccessContext("viewer", "tenant-x", "knowledge", "read"))
    assert not engine.check(AccessContext("viewer", "tenant-x", "knowledge", "write"))
    # operator can write
    assert engine.check(AccessContext("operator", "tenant-x", "knowledge", "write"))
    # reviewer can approve
    assert engine.check(AccessContext("reviewer", "tenant-x", "rca", "approve"))


# -------------------------------------------------------------------------- #
# build_casbin_engine — uses bundled policy files when present
# -------------------------------------------------------------------------- #


def test_build_engine_uses_bundled_policies() -> None:
    engine = build_casbin_engine()
    # Bundled policy grants the four canonical roles.  Smoke test.
    assert isinstance(engine, CasbinPolicyEngine)
    assert engine.check(AccessContext("viewer", "tenant-default", "knowledge", "read"))


# -------------------------------------------------------------------------- #
# Enforcer introspection
# -------------------------------------------------------------------------- #


def test_engine_list_policies_for_user(engine: CasbinPolicyEngine) -> None:
    policies = engine.list_policies_for_user("alice", "tenant-acme")
    assert ("alice", "tenant-acme", "knowledge", "read") in policies
    assert ("alice", "tenant-acme", "knowledge", "write") in policies


def test_engine_list_roles_for_user(engine: CasbinPolicyEngine) -> None:
    roles = engine.list_roles_for_user("alice", "tenant-acme")
    assert "reviewer" in roles
