"""Per-template rate limit tests.

Extends :mod:`rate_limit` so each agent template (rca, change_assessment,
knowledge_qa, etc.) gets its own token-bucket.  When ``rca`` is configured
with rate=2 / burst=2, a single client can fire 2 RCA requests in a
second but the 3rd is throttled — meanwhile the same client can still
fire ``knowledge_qa`` calls because that template has a separate bucket.
"""
from __future__ import annotations

import json

import pytest

from ai_employee.agent_platform_api.rate_limit import (
    PerTemplateLimiter,
    parse_template_rate_limit_env,
    template_key_for_request,
)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def test_parse_empty_env_returns_empty_dict() -> None:
    assert parse_template_rate_limit_env("") == {}


def test_parse_single_template() -> None:
    cfg = parse_template_rate_limit_env("rca:30,5")
    assert cfg == {"rca": (30, 5)}


def test_parse_multiple_templates() -> None:
    cfg = parse_template_rate_limit_env("rca:30,5;change_assessment:10,2;knowledge_qa:120,20")
    assert cfg == {
        "rca": (30, 5),
        "change_assessment": (10, 2),
        "knowledge_qa": (120, 20),
    }


def test_parse_trims_whitespace() -> None:
    cfg = parse_template_rate_limit_env("  rca: 30 , 5 ; knowledge_qa : 120,20 ")
    assert cfg["rca"] == (30, 5)
    assert cfg["knowledge_qa"] == (120, 20)


def test_parse_ignores_malformed_segments() -> None:
    cfg = parse_template_rate_limit_env("rca:30,5;bad;change_assessment:abc,def;knowledge_qa:120,20")
    assert cfg == {"rca": (30, 5), "knowledge_qa": (120, 20)}


# --------------------------------------------------------------------------- #
# template_key_for_request
# --------------------------------------------------------------------------- #


def test_template_key_includes_template_id() -> None:
    a = template_key_for_request(
        template_id="rca", claims_sub="alice", remote_addr=None,
    )
    b = template_key_for_request(
        template_id="knowledge_qa", claims_sub="alice", remote_addr=None,
    )
    assert a != b
    assert a.startswith("template:rca:")


def test_template_key_falls_back_to_ip() -> None:
    a = template_key_for_request(
        template_id="rca", claims_sub=None, remote_addr="10.0.0.1",
    )
    assert "ip:10.0.0.1" in a


# --------------------------------------------------------------------------- #
# PerTemplateLimiter behaviour
# --------------------------------------------------------------------------- #


def test_per_template_limiter_separates_buckets_per_template() -> None:
    limiter = PerTemplateLimiter(
        template_rates={"rca": (2, 2), "knowledge_qa": (10, 10)},
        default_rate=(60, 10),
    )
    # Drain the RCA bucket.
    assert limiter.allow_for_template("rca", "alice").allowed
    assert limiter.allow_for_template("rca", "alice").allowed
    third = limiter.allow_for_template("rca", "alice")
    assert third.allowed is False
    # Same user can still hit knowledge_qa.
    assert limiter.allow_for_template("knowledge_qa", "alice").allowed


def test_per_template_limiter_uses_default_for_unknown_template() -> None:
    limiter = PerTemplateLimiter(
        template_rates={"rca": (2, 2)},
        default_rate=(60, 60),
    )
    decision = limiter.allow_for_template("unknown_template", "alice")
    assert decision.allowed is True
    assert decision.remaining == 59


def test_per_template_limiter_separates_per_subject() -> None:
    limiter = PerTemplateLimiter(
        template_rates={"rca": (2, 2)},
        default_rate=(60, 60),
    )
    # Drain alice's bucket.
    limiter.allow_for_template("rca", "alice")
    limiter.allow_for_template("rca", "alice")
    assert limiter.allow_for_template("rca", "alice").allowed is False
    # Bob is unaffected.
    assert limiter.allow_for_template("rca", "bob").allowed is True


def test_per_template_limiter_reset_clears_state() -> None:
    limiter = PerTemplateLimiter(
        template_rates={"rca": (2, 2)},
        default_rate=(60, 60),
    )
    limiter.allow_for_template("rca", "alice")
    limiter.reset()
    # After reset, alice can request again.
    assert limiter.allow_for_template("rca", "alice").allowed


def test_per_template_limiter_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RATE_LIMIT_PER_TEMPLATE",
        "rca:30,5;knowledge_qa:120,20",
    )
    limiter = PerTemplateLimiter.from_env()
    # Both templates have been parsed.
    assert limiter.template_rates["rca"] == (30, 5)
    assert limiter.template_rates["knowledge_qa"] == (120, 20)
