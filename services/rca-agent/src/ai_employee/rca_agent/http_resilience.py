"""R25-T.4: RCA adapter HTTP resilience — timeout + retry on _HttpAdapter fetch.

Adds :func:`resilient_fetch` that wraps any callable with:

* **Timeout**: thread-based wall-clock enforcement (hard abort).
* **Retry**: platform ``RetryPolicy`` with env-overridable knobs
  (``RL_HTTP_RETRY_MAX_ATTEMPTS``, ``RL_HTTP_RETRY_BACKOFF_SECONDS``).

The ``_HttpAdapter._get`` method is updated to delegate through this
wrapper so all four real adapters (Prometheus, Elasticsearch, Neo4j,
Ticket API) automatically gain resilience.

Defaults are backward-compatible: ``max_attempts=1``, ``timeout``
passed through from the adapter's constructor.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class _FetchTimeoutError(Exception):
    """Sentinel raised when the fetch op exceeds its time budget."""


def _read_env_retry() -> tuple[int, float]:
    """Return ``(max_attempts, backoff_seconds)`` from env, defaulting to
    (1, 0.0)."""
    max_att = 1
    backoff = 0.0
    raw_max = os.getenv("RL_HTTP_RETRY_MAX_ATTEMPTS")
    if raw_max is not None:
        try:
            max_att = max(1, int(raw_max))
        except (ValueError, TypeError):
            pass
    raw_backoff = os.getenv("RL_HTTP_RETRY_BACKOFF_SECONDS")
    if raw_backoff is not None:
        try:
            backoff = max(0.0, float(raw_backoff))
        except (ValueError, TypeError):
            pass
    return max_att, backoff


def resilient_fetch(op: Callable[[], T], *, timeout_ms: int = 5000) -> T:
    """Run ``op`` with a hard timeout and optional retry.

    * ``timeout_ms``: wall-clock budget per attempt.  0 disables.
    * Retry knobs read from env:
      ``RL_HTTP_RETRY_MAX_ATTEMPTS`` (default 1 → single shot)
      ``RL_HTTP_RETRY_BACKOFF_SECONDS`` (default 0.0)
    * On timeout, raises ``_FetchTimeoutError``, which the adapter
      translate into ``AdapterUnavailable``.
    """
    max_attempts, backoff = _read_env_retry()

    import time as _time

    for attempt in range(1, max_attempts + 1):
        holder: dict[str, Any] = {}

        def _runner() -> None:
            try:
                holder["result"] = op()
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc

        worker = threading.Thread(target=_runner, daemon=True)
        worker.start()
        if timeout_ms > 0:
            worker.join(timeout=max(0.001, timeout_ms / 1000.0))
        else:
            worker.join()
        if worker.is_alive():
            if attempt < max_attempts:
                if backoff > 0:
                    _time.sleep(backoff)
                continue
            raise _FetchTimeoutError(
                f"fetch timed out after {timeout_ms}ms ({attempt} attempt(s))"
            )
        if "error" in holder:
            exc = holder["error"]
            if attempt < max_attempts:
                if backoff > 0:
                    _time.sleep(backoff)
                continue
            # Exhausted retries — wrap in AdapterUnavailable so callers
            # always get a consistent exception type, regardless of the
            # original cause.
            from ai_employee.rca_agent.tool_adapters import (
                AdapterUnavailable,
            )

            if isinstance(exc, _FetchTimeoutError):
                raise AdapterUnavailable(str(exc)) from exc
            raise AdapterUnavailable(
                f"fetch failed after {attempt} attempt(s): {exc}"
            ) from exc
        return holder["result"]  # type: ignore[return-value]

    # Defensive — should not reach here.
    raise _FetchTimeoutError("unreachable")
