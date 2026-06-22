"""Rate limiter tests."""

from __future__ import annotations

from ai_employee.agent_platform_api.rate_limit import (
    TokenBucketLimiter,
    build_limiter,
    key_for_request,
)


def test_limiter_allows_within_burst() -> None:
    lim = TokenBucketLimiter(rate_per_minute=60, burst=3)
    for _ in range(3):
        d = lim.allow("k1")
        assert d.allowed
    # Fourth within the same instant exceeds the burst.
    d = lim.allow("k1")
    assert not d.allowed
    assert d.reset_seconds > 0


def test_limiter_refills_over_time() -> None:
    lim = TokenBucketLimiter(rate_per_minute=60, burst=1)
    assert lim.allow("k").allowed
    # Without time passing, the next call should be rejected.
    assert not lim.allow("k").allowed


def test_limiter_isolates_keys() -> None:
    lim = TokenBucketLimiter(rate_per_minute=60, burst=1)
    assert lim.allow("alice").allowed
    assert not lim.allow("alice").allowed
    # bob has his own bucket.
    assert lim.allow("bob").allowed


def test_limiter_reset_clears_bucket() -> None:
    lim = TokenBucketLimiter(rate_per_minute=60, burst=1)
    lim.allow("k")
    assert not lim.allow("k").allowed
    lim.reset("k")
    assert lim.allow("k").allowed


def test_disabled_limiter_always_allows(monkeypatch) -> None:
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    lim = build_limiter()
    for _ in range(100):
        assert lim.allow("k").allowed


def test_enabled_limiter_built_from_env(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "120")
    monkeypatch.setenv("RATE_LIMIT_BURST", "5")
    lim = build_limiter()
    assert lim.rate == 120
    assert lim.burst == 5


def test_key_for_request_prefers_subject() -> None:
    assert key_for_request("alice", "1.2.3.4") == "sub:alice"
    assert key_for_request(None, "1.2.3.4") == "ip:1.2.3.4"
    assert key_for_request(None, None) == "ip:unknown"
