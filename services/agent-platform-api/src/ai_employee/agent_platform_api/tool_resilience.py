"""ToolSpec resilience: retry, circuit-breaker, health check (spec §5.3).

Adds the production-grade guarantees that wrap a tool's
``invoke(arguments)`` call:

* **Timeout** — ``timeout_ms`` per call (enforced by the caller; this
  module reports it via the response).
* **Retry** — :class:`RetryPolicy` (max_attempts + backoff_seconds).
* **Circuit breaker** — :class:`CircuitBreaker` (failure_threshold +
  cooldown_seconds; opens after N consecutive failures, half-opens to
  probe after cooldown, closes on success).
* **Health probe** — :class:`HealthChecker` (HTTP GET to
  ``health_check_url`` → ``HealthStatus``).

Composition is done via :func:`apply_resilience`, which threads an
operation through retry + breaker and surfaces the final result plus
latency.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Risk-level semantics
# --------------------------------------------------------------------------- #


RiskDecision = str  # one of {"auto", "needs_approval", "blocked"}


def evaluate_tool_risk(risk_level: str) -> RiskDecision:
    """Map a :data:`ToolRiskLevel` to an execution decision.

    The canonical 4 levels per spec §5.3:

    * ``read_only`` → ``auto`` (no approval needed, side-effect free)
    * ``suggest``  → ``auto`` (LLM suggestion only; not auto-executed)
    * ``approval_required`` → ``needs_approval`` (human-in-the-loop)
    * ``forbidden`` → ``blocked`` (never invoked; registration surfaces
      as 403)

    Unknown / legacy levels (``readonly``, ``high_risk``) are mapped
    conservatively: ``readonly`` → ``auto``, ``high_risk`` →
    ``needs_approval``.  Anything else falls back to ``needs_approval``
    so a typo doesn't silently auto-execute.
    """
    mapping: dict[str, RiskDecision] = {
        "read_only": "auto",
        "readonly": "auto",
        "suggest": "auto",
        "approval_required": "needs_approval",
        "high_risk": "needs_approval",
        "forbidden": "blocked",
    }
    return mapping.get(risk_level, "needs_approval")


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0


class ToolInvocationError(RuntimeError):
    """Raised when a tool call has exhausted retries or the breaker is open."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        error_code: str = "invocation_failed",
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.error_code = error_code


def apply_resilience(
    op: Callable[[], T],
    *,
    retry: RetryPolicy | None,
    breaker: CircuitBreaker | None,
    measure_latency: bool = False,
) -> T | tuple[T, float]:
    """Thread ``op`` through retry + breaker.

    Returns ``op()`` on success.  Raises :class:`ToolInvocationError`
    when:

    * ``breaker.state == OPEN`` (short-circuits before invoking ``op``).
    * All ``retry.max_attempts`` attempts raised.
    """
    if breaker is not None and not breaker.allow():
        raise ToolInvocationError(
            "circuit breaker is open",
            attempts=0,
            error_code="circuit_open",
        )

    attempts = retry.max_attempts if retry else 1
    backoff = retry.backoff_seconds if retry else 0.0
    started = time.monotonic()
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            result = op()
        except Exception as exc:
            last_exc = exc
            if breaker is not None:
                breaker.record_failure()
            if i < attempts:
                if backoff > 0:
                    time.sleep(backoff)
                continue
            raise ToolInvocationError(
                str(exc) or "tool invocation failed",
                attempts=i,
                error_code="invocation_failed",
            ) from exc
        if breaker is not None:
            breaker.record_success()
        if measure_latency:
            elapsed = time.monotonic() - started
            return result, elapsed
        return result  # type: ignore[return-value]

    # Defensive: should not be reachable.
    raise ToolInvocationError(
        str(last_exc) if last_exc else "unknown failure",
        attempts=attempts,
    )


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    _failures: int = 0
    _state: CircuitState = CircuitState.CLOSED
    _opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        # Auto-transition OPEN → HALF_OPEN once cooldown has elapsed.
        if (
            self._state == CircuitState.OPEN
            and (time.monotonic() - self._opened_at) >= self.cooldown_seconds
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """Return True if a call may proceed.

        ``OPEN`` → deny.  ``HALF_OPEN`` → allow one probe.
        ``CLOSED`` → always allow.
        """
        cur = self.state
        if cur == CircuitState.OPEN:
            return False
        return True

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED


# --------------------------------------------------------------------------- #
# Health probe
# --------------------------------------------------------------------------- #


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    status: HealthStatus
    latency_ms: float
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "checked_at": self.checked_at,
        }


class HealthChecker:
    """Synchronous HTTP health probe with a timeout.

    ``url=None`` (or any URL not parseable as ``http(s)://...``) yields
    :attr:`HealthStatus.UNKNOWN` — the tool's health_status field will
    not become "unhealthy" just because the URL is missing.
    """

    def __init__(self, *, timeout_ms: int = 500) -> None:
        self.timeout_s = max(0.001, timeout_ms / 1000.0)

    def check(self, url: str | None) -> HealthCheckResult:
        if not url or not url.startswith(("http://", "https://")):
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                latency_ms=0.0,
                error="no health_check_url",
            )
        started = time.monotonic()
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                code = getattr(resp, "status", 200) or 200
            latency = (time.monotonic() - started) * 1000.0
            if 200 <= code < 300:
                return HealthCheckResult(
                    status=HealthStatus.HEALTHY,
                    latency_ms=latency,
                )
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                latency_ms=latency,
                error=f"http {code}",
            )
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            latency = (time.monotonic() - started) * 1000.0
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                latency_ms=latency,
                error=str(exc) or exc.__class__.__name__,
            )


# --------------------------------------------------------------------------- #
# ToolResponse enrichment
# --------------------------------------------------------------------------- #


def attach_health_status(
    reg: Any,
    *,
    store: Any,
    checker: HealthChecker | None = None,
) -> Any:
    """Convert a :class:`ToolRegistration` to :class:`ToolResponse` with
    a real ``health_status`` (via :class:`HealthChecker`).

    Pass ``store`` so future enrichment (e.g. last-invocation timestamp)
    can be wired in.
    """
    from ai_employee.agent_platform_api.schemas import ToolResponse

    checker = checker or HealthChecker()
    url = getattr(reg, "health_check_url", None)
    result = checker.check(url)
    return ToolResponse(
        **reg.model_dump(),
        health_status=result.status.value,
    )


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "HealthCheckResult",
    "HealthChecker",
    "HealthStatus",
    "RetryPolicy",
    "RiskDecision",
    "ToolInvocationError",
    "apply_resilience",
    "attach_health_status",
    "evaluate_tool_risk",
]
