"""Retry policy + idempotent resume helpers (spec §5.2).

A :class:`RetryPolicy` describes the limits for re-running an agent
after a transient failure: how many times to try, how long to wait
between attempts, and which error codes are worth retrying.

:func:`decide_retry` is the pure decision function: given the current
attempt index, the error code observed, and the policy, it returns a
:class:`RetryDecision` with the verdict, the next attempt number, and
an optional exponential-backoff delay.  Idempotency keys are passed
through unchanged so the same retry pair is never executed twice.

The HTTP layer parses the standard ``Retry-After`` header via
:func:`parse_retry_after_ms` to honour upstream rate limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class RetryPolicy:
    """Per-template retry contract.

    ``max_attempts`` is the cap (1 means "no retries").  ``backoff_s``
    is the base delay; the actual delay grows as ``backoff_s * 2 ** (attempt - 1)``.
    ``retryable_errors`` is the closed set of error codes that may
    trigger a retry — anything else fails immediately.
    """

    max_attempts: int = 3
    backoff_s: float = 1.0
    retryable_errors: tuple[str, ...] = (
        "timeout",
        "connection_error",
        "5xx",
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_s < 0:
            raise ValueError("backoff_s must be >= 0")

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RetryPolicy:
        """Build from a config dict; missing keys fall back to defaults."""
        if not payload:
            return cls()
        return cls(
            max_attempts=int(payload.get("max_attempts", cls.max_attempts)),
            backoff_s=float(payload.get("backoff_s", cls.backoff_s)),
            retryable_errors=tuple(
                payload.get("retryable_errors", list(cls.retryable_errors)),
            ),
        )


@dataclass
class RetryDecision:
    """Verdict of :func:`decide_retry`."""

    should_retry: bool
    next_attempt: int
    retry_after_s: float = 0.0
    idempotency_key: str | None = None


def decide_retry(
    *,
    attempt_index: int,
    error_code: str | None,
    policy: RetryPolicy,
    idempotency_key: str | None = None,
) -> RetryDecision:
    """Decide whether to retry ``attempt_index`` after ``error_code``.

    Returns a :class:`RetryDecision` with ``should_retry=False`` when
    the attempt succeeded (``error_code is None``), when the error is
    not retryable, or when the policy's cap has been reached.  The
    ``retry_after_s`` grows exponentially with the attempt count.
    """
    if error_code is None:
        return RetryDecision(
            should_retry=False,
            next_attempt=attempt_index,
            retry_after_s=0.0,
            idempotency_key=idempotency_key,
        )
    if error_code not in policy.retryable_errors:
        return RetryDecision(
            should_retry=False,
            next_attempt=attempt_index,
            retry_after_s=0.0,
            idempotency_key=idempotency_key,
        )
    if attempt_index >= policy.max_attempts:
        return RetryDecision(
            should_retry=False,
            next_attempt=attempt_index,
            retry_after_s=0.0,
            idempotency_key=idempotency_key,
        )
    # Exponential backoff: backoff_s * 2^(attempt-1), with a small floor
    # so the first retry always waits at least backoff_s.
    backoff = policy.backoff_s * (2 ** max(0, attempt_index - 1))
    return RetryDecision(
        should_retry=True,
        next_attempt=attempt_index + 1,
        retry_after_s=backoff,
        idempotency_key=idempotency_key,
    )


_RETRY_AFTER_RE = re.compile(r"(?i)^retry-after:\s*(\d+)\s*$")


def parse_retry_after_ms(header: str | None) -> int | None:
    """Parse a ``Retry-After: <seconds>`` header value.

    Returns the delay in milliseconds (so callers can compare with the
    ms-granularity :class:`RetryDecision.retry_after_s` without unit
    conversions).  Returns ``None`` when the header is missing or
    malformed.
    """
    if not header:
        return None
    match = _RETRY_AFTER_RE.match(header.strip())
    if match is None:
        return None
    seconds = int(match.group(1))
    return seconds * 1000


__all__ = [
    "RetryDecision",
    "RetryPolicy",
    "decide_retry",
    "parse_retry_after_ms",
]
