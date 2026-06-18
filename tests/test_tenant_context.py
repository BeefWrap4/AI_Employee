"""Tenant context resolution tests."""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.tenant import (
    TenantContext,
    parse_tenant_from_subject,
    resolve_tenant_context,
)


# --------------------------------------------------------------------------- #
# parse_tenant_from_subject
# --------------------------------------------------------------------------- #


def test_parse_tenant_from_subject_with_prefix() -> None:
    assert parse_tenant_from_subject("acme:alice") == "acme"


def test_parse_tenant_from_subject_no_prefix_returns_default() -> None:
    assert parse_tenant_from_subject("alice", default="public") == "public"


def test_parse_tenant_from_subject_empty_returns_default() -> None:
    assert parse_tenant_from_subject("", default="public") == "public"


def test_parse_tenant_from_subject_multi_colon_uses_first() -> None:
    assert parse_tenant_from_subject("acme:team:alice") == "acme"


# --------------------------------------------------------------------------- #
# resolve_tenant_context
# --------------------------------------------------------------------------- #


def test_resolve_explicit_query_wins() -> None:
    ctx = resolve_tenant_context(
        explicit="acme",
        header_tenant=None,
        claims_sub="globex:alice",
    )
    assert ctx.tenant_id == "acme"
    assert ctx.source == "query"


def test_resolve_header_wins_over_subject() -> None:
    ctx = resolve_tenant_context(
        explicit=None,
        header_tenant="acme",
        claims_sub="globex:alice",
    )
    assert ctx.tenant_id == "acme"
    assert ctx.source == "header"


def test_resolve_subject_prefix_when_no_explicit_or_header() -> None:
    ctx = resolve_tenant_context(
        explicit=None,
        header_tenant=None,
        claims_sub="acme:alice",
    )
    assert ctx.tenant_id == "acme"
    assert ctx.source == "subject"


def test_resolve_default_when_nothing_else() -> None:
    ctx = resolve_tenant_context(
        explicit=None,
        header_tenant=None,
        claims_sub="alice",
    )
    assert ctx.tenant_id == "public"
    assert ctx.source == "default"


def test_resolve_explicit_empty_string_falls_through() -> None:
    """An empty explicit value should not be treated as a real tenant."""
    ctx = resolve_tenant_context(
        explicit="",
        header_tenant=None,
        claims_sub="acme:alice",
    )
    assert ctx.tenant_id == "acme"


def test_resolve_rejects_invalid_tenant_id() -> None:
    """Tenant IDs must be alphanumeric / underscore / dash, <= 64 chars."""
    with pytest.raises(ValueError):
        resolve_tenant_context(
            explicit="bad tenant with spaces",
            header_tenant=None,
            claims_sub=None,
        )


def test_resolve_rejects_overlong_tenant_id() -> None:
    with pytest.raises(ValueError):
        resolve_tenant_context(
            explicit="a" * 65,
            header_tenant=None,
            claims_sub=None,
        )


def test_resolve_accepts_alphanumeric_underscore_dash() -> None:
    ctx = resolve_tenant_context(
        explicit="acme-corp_2024",
        header_tenant=None,
        claims_sub=None,
    )
    assert ctx.tenant_id == "acme-corp_2024"


# --------------------------------------------------------------------------- #
# TenantContext dataclass
# --------------------------------------------------------------------------- #


def test_tenant_context_to_dict() -> None:
    ctx = TenantContext(tenant_id="acme", user_id="alice", source="subject")
    d = ctx.to_dict()
    assert d == {"tenant_id": "acme", "user_id": "alice", "source": "subject"}


def test_tenant_context_user_id_optional() -> None:
    ctx = TenantContext(tenant_id="acme", user_id=None, source="default")
    assert ctx.user_id is None


def test_resolve_attaches_user_id_from_subject() -> None:
    ctx = resolve_tenant_context(
        explicit=None, header_tenant=None, claims_sub="acme:alice",
    )
    assert ctx.user_id == "alice"