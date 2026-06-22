"""Casbin RBAC policy engine (spec P3 §4 Casbin 或企业权限系统).

Wraps the casbin Enforcer with a small adapter that gives the
existing rbac API a 1:1 superset of functionality:

  ``casbin.check(AccessContext(sub, dom, obj, act))``  -> bool
  ``casbin.enforce(ctx)``                              -> raises on deny
  ``casbin.add_policy / remove_policy / add_role_for_user``
  ``casbin.list_policies_for_user / list_roles_for_user``
  ``casbin.reload()`` — hot-swap policy.csv from disk

The model is RBAC-with-domains (sub, dom, obj, act) so we can scope
permissions per tenant — matching the :class:`TenantContext` work
from R10-3.  Policy is loaded from ``policy.csv`` either in memory
(tests) or from disk (production).  ``build_casbin_engine`` wires the
bundled model + policy under ``packages/auth-policy/policies/``.

A drop-in for :func:`rbac.can` is provided so services can migrate
incrementally without changing call sites.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import casbin

# --------------------------------------------------------------------------- #
# Bundled model + policy (committed under packages/auth-policy/policies/)
# --------------------------------------------------------------------------- #

# Bundled under packages/auth-policy/policies/.  Resolve relative to
# this file but climb up out of the src/ tree.
_POLICY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "policies"

BUNDLED_MODEL = """\
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

# Default policies carry over the four roles from rbac.py so the
# Casbin engine is a drop-in for existing services.
BUNDLED_POLICY = """\
# --- knowledge ---------------------------------------------------------
p, viewer,   tenant-default, knowledge, read
p, operator, tenant-default, knowledge, read
p, operator, tenant-default, knowledge, write
p, admin,    *,              knowledge, *

# --- rca ---------------------------------------------------------------
p, viewer,   tenant-default, rca, read
p, operator, tenant-default, rca, read
p, operator, tenant-default, rca, write
p, reviewer, tenant-default, rca, approve
p, admin,    *,              rca, *

# --- agent -------------------------------------------------------------
p, operator, tenant-default, agent, run
p, reviewer, tenant-default, agent, approve
p, admin,    *,              agent, *

# --- tool --------------------------------------------------------------
p, operator, tenant-default, tool, invoke
p, admin,    *,              tool, *

# --- inspect -----------------------------------------------------------
p, operator, tenant-default, inspect, run
p, admin,    *,              inspect, *

# --- role bindings ------------------------------------------------------
g, reviewer, operator
"""


# --------------------------------------------------------------------------- #
# AccessContext
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AccessContext:
    """A single (sub, dom, obj, act) request for the policy engine."""

    sub: str
    dom: str
    obj: str
    act: str

    def to_request(self) -> tuple[str, str, str, str]:
        return (self.sub, self.dom, self.obj, self.act)


# --------------------------------------------------------------------------- #
# Engine wrapper
# --------------------------------------------------------------------------- #


class CasbinPolicyEngine:
    """Thin wrapper around casbin.Enforcer with helpers for our domain."""

    def __init__(
        self,
        *,
        model_path: str,
        policy_path: str,
    ) -> None:
        self._model_path = model_path
        self._policy_path = policy_path
        self._enforcer = casbin.Enforcer(model_path, policy_path)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def from_text(cls, model_text: str, policy_text: str) -> CasbinPolicyEngine:
        """Build an in-memory engine from raw strings (for tests)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp) / "model.conf"
            p = Path(tmp) / "policy.csv"
            m.write_text(model_text)
            p.write_text(policy_text)
            return cls(model_path=str(m), policy_path=str(p))

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def check(self, ctx: AccessContext) -> bool:
        return bool(self._enforcer.enforce(*ctx.to_request()))

    def enforce(self, ctx: AccessContext) -> None:
        if not self.check(ctx):
            raise PermissionError(
                f"permission denied: sub={ctx.sub!r} dom={ctx.dom!r} "
                f"obj={ctx.obj!r} act={ctx.act!r}"
            )

    def check_all(self, contexts: Iterable[AccessContext]) -> list[bool]:
        return [self.check(c) for c in contexts]

    # ------------------------------------------------------------------ #
    # Policy mutation
    # ------------------------------------------------------------------ #

    def add_policy(self, sub: str, dom: str, obj: str, act: str) -> None:
        self._enforcer.add_policy(sub, dom, obj, act)

    def remove_policy(self, sub: str, dom: str, obj: str, act: str) -> None:
        self._enforcer.remove_policy(sub, dom, obj, act)

    def add_role_for_user(self, user: str, role: str, dom: str) -> None:
        self._enforcer.add_grouping_policy(user, role, dom)

    def remove_role_for_user(self, user: str, role: str, dom: str) -> None:
        self._enforcer.remove_grouping_policy(user, role, dom)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def list_policies_for_user(self, sub: str, dom: str) -> list[tuple[str, str, str, str]]:
        """All direct policies for ``(sub, dom)`` (not inherited via roles)."""
        out: list[tuple[str, str, str, str]] = []
        for vals in self._enforcer.get_named_policy("p"):
            if len(vals) >= 4 and vals[0] == sub and vals[1] == dom:
                out.append(tuple(vals[:4]))
        return out

    def list_roles_for_user(self, sub: str, dom: str) -> list[str]:
        roles: list[str] = []
        for vals in self._enforcer.get_named_grouping_policy("g"):
            if len(vals) >= 3 and vals[0] == sub and vals[2] == dom:
                roles.append(vals[1])
        return roles

    # ------------------------------------------------------------------ #
    # Hot reload
    # ------------------------------------------------------------------ #

    def reload(self) -> None:
        """Re-read the policy file and rebuild the enforcer."""
        self._enforcer = casbin.Enforcer(self._model_path, self._policy_path)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_casbin_engine(
    *,
    model_path: str | None = None,
    policy_path: str | None = None,
) -> CasbinPolicyEngine:
    """Build the engine, defaulting to the bundled model + policy."""
    m = model_path if model_path is not None else str(_POLICY_DIR / "model.conf")
    p = policy_path if policy_path is not None else str(_POLICY_DIR / "policy.csv")
    return CasbinPolicyEngine(model_path=m, policy_path=p)


# --------------------------------------------------------------------------- #
# rbac.can() drop-in
# --------------------------------------------------------------------------- #


def _roles_to_subjects(roles: list[str]) -> list[str]:
    """Map rbac role names to Casbin subjects (roles appear as p.sub)."""
    return list(roles)


def casbin_check(
    engine: CasbinPolicyEngine,
    *,
    sub: str,
    dom: str,
    obj: str,
    act: str,
) -> bool:
    return engine.check(AccessContext(sub, dom, obj, act))


__all__ = [
    "BUNDLED_MODEL",
    "BUNDLED_POLICY",
    "AccessContext",
    "CasbinPolicyEngine",
    "build_casbin_engine",
    "casbin_check",
]
