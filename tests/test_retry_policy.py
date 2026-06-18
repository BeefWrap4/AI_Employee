"""Retry policy + idempotent resume tests."""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.retry import (
    RetryDecision,
    RetryPolicy,
    decide_retry,
    parse_retry_after_ms,
)


# --------------------------------------------------------------------------- #
# RetryPolicy parsing
# --------------------------------------------------------------------------- #


def test_retry_policy_defaults() -> None:
    p = RetryPolicy()
    assert p.max_attempts == 3
    assert p.backoff_s == 1.0
    assert p.retryable_errors == ("timeout", "connection_error", "5xx")


def test_retry_policy_from_dict_overrides() -> None:
    p = RetryPolicy.from_dict({
        "max_attempts": 5,
        "backoff_s": 0.5,
        "retryable_errors": ["timeout"],
    })
    assert p.max_attempts == 5
    assert p.backoff_s == 0.5
    assert p.retryable_errors == ("timeout",)


def test_retry_policy_from_partial_dict_uses_defaults() -> None:
    p = RetryPolicy.from_dict({"max_attempts": 7})
    assert p.max_attempts == 7
    assert p.backoff_s == 1.0


def test_retry_policy_invalid_max_attempts_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


# --------------------------------------------------------------------------- #
# decide_retry
# --------------------------------------------------------------------------- #


def test_decide_retry_no_error_stops_after_success() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_s=1.0)
    decision = decide_retry(
        attempt_index=1,
        error_code=None,
        policy=policy,
    )
    assert decision.should_retry is False


def test_decide_retry_retryable_error_before_max() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_s=1.0, retryable_errors=("timeout",))
    decision = decide_retry(
        attempt_index=1,
        error_code="timeout",
        policy=policy,
    )
    assert decision.should_retry is True
    assert decision.next_attempt == 2
    assert decision.retry_after_s >= 1.0


def test_decide_retry_retryable_error_at_max_stops() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_s=1.0, retryable_errors=("timeout",))
    decision = decide_retry(
        attempt_index=3,
        error_code="timeout",
        policy=policy,
    )
    assert decision.should_retry is False


def test_decide_retry_non_retryable_error_stops() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_s=1.0, retryable_errors=("timeout",))
    decision = decide_retry(
        attempt_index=1,
        error_code="validation_error",
        policy=policy,
    )
    assert decision.should_retry is False


def test_decide_retry_exponential_backoff() -> None:
    policy = RetryPolicy(max_attempts=5, backoff_s=1.0, retryable_errors=("timeout",))
    d1 = decide_retry(attempt_index=1, error_code="timeout", policy=policy)
    d2 = decide_retry(attempt_index=2, error_code="timeout", policy=policy)
    d3 = decide_retry(attempt_index=3, error_code="timeout", policy=policy)
    # Each attempt should wait at least as long as the previous one.
    assert d1.retry_after_s < d2.retry_after_s
    assert d2.retry_after_s < d3.retry_after_s


def test_decide_retry_idempotency_key_is_deterministic() -> None:
    policy = RetryPolicy()
    a = decide_retry(
        attempt_index=1, error_code="timeout", policy=policy,
        idempotency_key="run-1:attempt-1",
    )
    b = decide_retry(
        attempt_index=1, error_code="timeout", policy=policy,
        idempotency_key="run-1:attempt-1",
    )
    assert a.idempotency_key == b.idempotency_key == "run-1:attempt-1"


# --------------------------------------------------------------------------- #
# parse_retry_after_ms
# --------------------------------------------------------------------------- #


def test_parse_retry_after_ms_valid_header() -> None:
    assert parse_retry_after_ms("retry-after: 5") == 5000
    assert parse_retry_after_ms("Retry-After: 12") == 12000


def test_parse_retry_after_ms_none_returns_none() -> None:
    assert parse_retry_after_ms(None) is None
    assert parse_retry_after_ms("") is None
    assert parse_retry_after_ms("garbage") is None


def test_retry_decision_dataclass_defaults() -> None:
    d = RetryDecision(should_retry=True, next_attempt=2, retry_after_s=1.0)
    assert d.idempotency_key is None
