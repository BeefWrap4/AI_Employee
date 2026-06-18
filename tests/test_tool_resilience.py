"""Platform ToolSpec resilience fields (spec §5.3) + health_check + 4 risk levels.

Adds the missing pieces to :class:`ToolRegistration`:
- canonical 4 risk levels (read_only / suggest / approval_required / forbidden)
- ``timeout_ms`` per call
- ``retry_policy`` (max_attempts, backoff_seconds)
- ``circuit_breaker`` (failure_threshold, cooldown_seconds)
- ``health_check_url`` → real ``health_status`` evaluation
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
from ai_employee.agent_platform_api.runtime import AgentPlatformStore
from ai_employee.agent_platform_api.schemas import (
    ToolRegistration,
    ToolResponse,
)
from ai_employee.agent_platform_api.tool_resilience import (
    CircuitBreaker,
    CircuitState,
    HealthChecker,
    HealthStatus,
    RetryPolicy,
    ToolInvocationError,
    apply_resilience,
    evaluate_tool_risk,
)

# --------------------------------------------------------------------------- #
# Risk level semantics
# --------------------------------------------------------------------------- #


def test_evaluate_tool_risk_4_levels() -> None:
    assert evaluate_tool_risk("read_only") == "auto"
    assert evaluate_tool_risk("suggest") == "auto"
    assert evaluate_tool_risk("approval_required") == "needs_approval"
    assert evaluate_tool_risk("forbidden") == "blocked"


def test_evaluate_tool_risk_unknown_defaults_to_approval() -> None:
    """Defensive: an unknown risk level should not silently auto-execute."""
    assert evaluate_tool_risk("???") == "needs_approval"


def test_canonical_4_levels_accepted_by_schema() -> None:
    """The schema accepts the canonical 4 levels (and aliases)."""
    for risk in ["read_only", "suggest", "approval_required", "forbidden"]:
        reg = ToolRegistration(
            tool_name=f"t-{risk}", service_name="x",
            description="x",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_level=risk,
        )
        assert reg.risk_level == risk


# --------------------------------------------------------------------------- #
# RetryPolicy + apply_resilience
# --------------------------------------------------------------------------- #


def test_retry_policy_succeeds_first_try() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_seconds=0.0)
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        return "ok"

    result = apply_resilience(op, retry=policy, breaker=None)
    assert result == "ok"
    assert calls["n"] == 1


def test_retry_policy_retries_until_success() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_seconds=0.0)
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    result = apply_resilience(op, retry=policy, breaker=None)
    assert result == "ok"
    assert calls["n"] == 2


def test_retry_policy_exhausted_raises() -> None:
    policy = RetryPolicy(max_attempts=2, backoff_seconds=0.0)
    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        raise RuntimeError("nope")

    with pytest.raises(ToolInvocationError) as ei:
        apply_resilience(op, retry=policy, breaker=None)
    assert calls["n"] == 2
    assert ei.value.attempts == 2


# --------------------------------------------------------------------------- #
# CircuitBreaker
# --------------------------------------------------------------------------- #


def test_circuit_breaker_opens_after_threshold_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10.0)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    # Now an invocation must short-circuit.
    policy = RetryPolicy(max_attempts=1, backoff_seconds=0.0)
    with pytest.raises(ToolInvocationError) as ei:
        apply_resilience(lambda: "x", retry=policy, breaker=breaker)
    assert ei.value.error_code == "circuit_open"


def test_circuit_breaker_half_open_after_cooldown() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0)
    breaker.record_failure()
    # Cooldown elapsed → next call should reset to CLOSED (half-open probe).
    breaker.allow()  # forces the time check
    assert breaker.state in (CircuitState.HALF_OPEN, CircuitState.CLOSED)


def test_circuit_breaker_resets_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10.0)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


# --------------------------------------------------------------------------- #
# HealthChecker
# --------------------------------------------------------------------------- #


def test_health_checker_synthetic_probe_healthy() -> None:
    """The checker can probe a ``health_check_url`` synchronously via urllib."""
    checker = HealthChecker(timeout_ms=500)
    # We don't actually want to hit the network — use the synthetic fallback
    # for unknown URLs (registered tools that haven't deployed yet).
    result = checker.check("unknown-svc")
    assert result.status in (HealthStatus.UNKNOWN, HealthStatus.HEALTHY)


def test_health_checker_loopback_skips_if_unreachable() -> None:
    """Loopback probe to a closed port is 'unhealthy' (TCP refused)."""
    checker = HealthChecker(timeout_ms=100)
    # 127.0.0.1:1 is effectively closed on any normal system.
    result = checker.check("http://127.0.0.1:1/health")
    assert result.status == HealthStatus.UNHEALTHY


def test_health_checker_loopback_2xx_is_healthy() -> None:
    """Spin a tiny HTTP server, expect 'healthy' from a 200 OK."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: Any, **kwargs: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        checker = HealthChecker(timeout_ms=1000)
        result = checker.check(f"http://127.0.0.1:{port}/health")
        assert result.status == HealthStatus.HEALTHY
        assert result.latency_ms >= 0.0
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Integration: ToolRegistration stores all resilience fields
# --------------------------------------------------------------------------- #


def test_tool_registration_carries_resilience_fields() -> None:
    from ai_employee.agent_platform_api.schemas import (
        CircuitBreakerModel,
        RetryPolicyModel,
    )

    reg = ToolRegistration(
        tool_name="net.switch",
        service_name="net",
        description="Apply switch config",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level="approval_required",
        health_check_url="http://net-svc/health",
        timeout_ms=2000,
        retry_policy=RetryPolicyModel(max_attempts=2, backoff_seconds=0.5),
        circuit_breaker=CircuitBreakerModel(failure_threshold=5, cooldown_seconds=60.0),
    )
    dumped = reg.model_dump()
    assert dumped["timeout_ms"] == 2000
    assert dumped["retry_policy"]["max_attempts"] == 2
    assert dumped["circuit_breaker"]["failure_threshold"] == 5


def test_tool_response_includes_runtime_health() -> None:
    """ToolResponse surfaces a real ``health_status`` (not just 'unknown')."""
    from ai_employee.agent_platform_api.tool_resilience import (
        attach_health_status,
    )

    checker = HealthChecker(timeout_ms=100)
    reg = ToolRegistration(
        tool_name="x", service_name="x", description="x",
        input_schema={"type": "object"}, output_schema={"type": "object"},
        risk_level="read_only",
        health_check_url="http://127.0.0.1:1/health",  # unreachable
    )
    store = AgentPlatformStore()
    response = attach_health_status(reg, store=store, checker=checker)
    assert isinstance(response, ToolResponse)
    assert response.health_status == "unhealthy"


def _make_tool_stub(reg: ToolRegistration) -> Any:
    """Build a minimal store tool entry mirroring the in-memory shape."""
    return type("T", (), {
        "tool_name": reg.tool_name,
        "service_name": reg.service_name,
        "description": reg.description,
        "input_schema": reg.input_schema,
        "output_schema": reg.output_schema,
        "risk_level": reg.risk_level,
        "status": "active",
        "health_status": "unknown",
        "timeout_ms": reg.timeout_ms,
        "retry_policy": reg.retry_policy,
        "circuit_breaker": reg.circuit_breaker,
        "health_check_url": reg.health_check_url,
    })()


def test_timeout_field_round_trips_json() -> None:
    reg = ToolRegistration(
        tool_name="x", service_name="x", description="x",
        input_schema={"type": "object"}, output_schema={"type": "object"},
        risk_level="read_only",
        timeout_ms=3000,
    )
    s = json.dumps(reg.model_dump(), default=str)
    assert "3000" in s
    # round-trip
    reloaded = ToolRegistration(**json.loads(s))
    assert reloaded.timeout_ms == 3000


# --------------------------------------------------------------------------- #
# Breaker + retry composition: latency-aware
# --------------------------------------------------------------------------- #


def test_apply_resilience_records_latency() -> None:
    policy = RetryPolicy(max_attempts=1, backoff_seconds=0.0)

    def op() -> str:
        time.sleep(0.05)
        return "ok"

    result, elapsed = apply_resilience(
        op, retry=policy, breaker=None, measure_latency=True,
    )
    assert result == "ok"
    assert elapsed >= 0.04  # 50ms sleep, allow scheduler slack
