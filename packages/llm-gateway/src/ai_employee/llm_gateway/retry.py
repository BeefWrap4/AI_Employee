"""Exponential-backoff retry decorator for transient HTTP errors."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

RETRYABLE_STATUSES: frozenset[int] = frozenset({429})


def _is_retryable(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUSES or status_code >= 500


class RetryExhaustedError(RuntimeError):
    """Raised when all retry attempts have been exhausted."""

    def __init__(self, message: str, last_status: int | None = None) -> None:
        super().__init__(message)
        self.last_status = last_status


def retry(
    max_retries: int = 2,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    retryable_check: Callable[[int], bool] | None = None,
) -> Callable[[F], F]:
    """Decorator: retry a function on transient HTTP errors with exponential backoff.

    The decorated function must return an ``httpx.Response`` object.
    Retryable statuses: 429, 5xx (customisable via *retryable_check*).
    Non-retryable 4xx (e.g. 401, 403) are re-raised immediately.

    Parameters
    ----------
    max_retries:
        Maximum number of retry attempts (default 2, so up to 3 total calls).
    base_delay:
        Initial backoff delay in seconds (default 0.2).
    max_delay:
        Maximum backoff delay cap in seconds (default 5.0).
    retryable_check:
        Optional callable(status_code) -> bool to override the default check.
    """

    check = retryable_check if retryable_check is not None else _is_retryable

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_status: int | None = None
            for attempt in range(max_retries + 1):
                try:
                    resp = func(*args, **kwargs)
                except Exception:
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        time.sleep(delay)
                        continue
                    raise
                if resp.status_code == 200:
                    return resp
                last_status = resp.status_code
                if not check(resp.status_code):
                    raise RetryExhaustedError(
                        f"non-retryable status {resp.status_code}: {getattr(resp, 'text', '')[:200]}",
                        last_status=resp.status_code,
                    )
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    time.sleep(delay)
                    continue
                raise RetryExhaustedError(
                    f"retries exhausted after status {resp.status_code}: {getattr(resp, 'text', '')[:200]}",
                    last_status=resp.status_code,
                )
            raise RetryExhaustedError(
                f"retries exhausted (last status {last_status})",
                last_status=last_status,
            )

        return wrapper  # type: ignore[return-value]

    return decorator
