"""R25-T.4: RCA adapter resilience — timeout + retry on _HttpAdapter fetch.

Each real adapter (PrometheusKPIAdapter, ElasticsearchLogAdapter,
Neo4jTopologyAdapter, TicketApiAdapter) extends ``_HttpAdapter`` which
bare-calls ``httpx.get`` with a single timeout.  R25-T.4 layers two
extra guards:

* **Timeout per attempt**: the adapter's ``timeout_seconds`` is now
  enforced in-process (thread-based) so a stuck handler truly aborts.
* **Retry**: the full fetch is wrapped with platform ``RetryPolicy``
  (default max_attempts=2, backoff_seconds=0.5, configurable via
  ``RETRY_MAX_ATTEMPTS`` / ``RETRY_BACKOFF_SECONDS`` env vars).

Backward compat: the default ``max_attempts=1`` means the adapter
behaves identically to the pre-R25 single-shot path (regression-free).
"""
from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from ai_employee.rca_agent.schemas import IncidentResponse, RawAlarmEvent
from ai_employee.rca_agent.tool_adapters import (
    AdapterUnavailable,
    PrometheusKPIAdapter,
    TicketApiAdapter,
    build_adapters,
)


def _make_incident() -> IncidentResponse:
    alarm = RawAlarmEvent(
        alarm_id="a_001",
        alarm_code="LINK_DEGRADE",
        alarm_name="Transmission link degradation",
        vendor="huawei",
        site_id="SITE-001",
        cell_id="CELL-001",
        ne_id="NE-001",
        severity="critical",
        start_time="2026-06-17T10:00:00+08:00",
        raw_payload={"port": "eth0/1"},
    )
    from ai_employee.rca_agent.schemas import AlarmEvent

    event = AlarmEvent(
        **alarm.model_dump(),
        alarm_event_id="alarm_evt_001",
        fingerprint=f"{alarm.vendor}:{alarm.site_id}:{alarm.ne_id}:{alarm.alarm_code}",
    )
    return IncidentResponse(
        incident_id="inc_001",
        title="SITE-001 Transmission link degradation",
        status="analyzing",
        severity="critical",
        site_id="SITE-001",
        primary_alarm=event,
        related_alarm_count=0,
        alarm_events=[event],
    )


# --------------------------------------------------------------------------- #
# Timeout: adapter respects a per-call budget
# --------------------------------------------------------------------------- #


def test_real_adapter_timeout_enforced(monkeypatch) -> None:
    """When the backing service hangs, the adapter aborts within its
    timeout_seconds budget (R25-T.4: timeout enforcement)."""
    from ai_employee.rca_agent.tool_adapters import PrometheusKPIAdapter

    def slow_get(*args: Any, **kwargs: Any) -> MagicMock:
        import time

        time.sleep(2.0)
        raise AssertionError("should have timed out")

    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    with patch("httpx.get", side_effect=slow_get):
        adapter = PrometheusKPIAdapter("http://prom.local:9090", timeout_seconds=0.1)
        with pytest.raises(AdapterUnavailable, match=r"(?i)timeout|unreachable"):
            adapter.fetch(_make_incident())


def test_real_adapter_default_timeout_is_backward_compatible(monkeypatch) -> None:
    """Without an explicit timeout override, the adapter uses its existing
    5.0s timeout — the behaviour is unchanged from pre-R25."""
    from ai_employee.rca_agent.tool_adapters import PrometheusKPIAdapter

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": {"result": [{"metric": {}}]}}
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    with patch("httpx.get", return_value=fake_resp):
        adapter = PrometheusKPIAdapter("http://prom.local:9090")
        evidence = adapter.fetch(_make_incident())
    assert len(evidence) == 1
    assert evidence[0].source_type == "metric"


# --------------------------------------------------------------------------- #
# Retry: transient failures get a second chance
# --------------------------------------------------------------------------- #


def test_real_adapter_retries_on_transient_failure(monkeypatch) -> None:
    """A single transient failure (e.g. connection refused) is retried
    once; the second attempt succeeds."""
    from ai_employee.rca_agent.tool_adapters import PrometheusKPIAdapter

    call_count = [0]

    def flaky_get(*args: Any, **kwargs: Any) -> MagicMock:
        call_count[0] += 1
        if call_count[0] == 1:
            import httpx as _hx

            raise _hx.ConnectError("refused")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": {"result": [{"metric": {}}]}}
        return resp

    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    monkeypatch.setenv("RL_HTTP_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("RL_HTTP_RETRY_BACKOFF_SECONDS", "0.0")
    with patch("httpx.get", side_effect=flaky_get):
        adapter = PrometheusKPIAdapter("http://prom.local:9090", timeout_seconds=5.0)
        evidence = adapter.fetch(_make_incident())
    assert len(evidence) == 1
    assert call_count[0] == 2


def test_real_adapter_retries_exhausted_raises(monkeypatch) -> None:
    """When all retry attempts fail, the adapter raises AdapterUnavailable."""
    from ai_employee.rca_agent.tool_adapters import PrometheusKPIAdapter

    call_count = [0]

    def always_fail(*args: Any, **kwargs: Any) -> None:
        call_count[0] += 1
        import httpx as _hx

        raise _hx.ConnectError("refused")

    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    monkeypatch.setenv("RL_HTTP_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("RL_HTTP_RETRY_BACKOFF_SECONDS", "0.0")
    with patch("httpx.get", side_effect=always_fail):
        adapter = PrometheusKPIAdapter("http://prom.local:9090", timeout_seconds=5.0)
        with pytest.raises(AdapterUnavailable):
            adapter.fetch(_make_incident())
    assert call_count[0] == 2


def test_real_adapter_default_retries_is_single_attempt(monkeypatch) -> None:
    """With no env override, the default retry_policy is max_attempts=1
    (one attempt = backward compatible with pre-R25 behaviour)."""
    from ai_employee.rca_agent.tool_adapters import PrometheusKPIAdapter

    call_count = [0]

    def fail_once(*args: Any, **kwargs: Any) -> None:
        call_count[0] += 1
        import httpx as _hx

        raise _hx.ConnectError("refused")

    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    monkeypatch.delenv("RL_HTTP_RETRY_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("RL_HTTP_RETRY_BACKOFF_SECONDS", raising=False)
    with patch("httpx.get", side_effect=fail_once):
        adapter = PrometheusKPIAdapter("http://prom.local:9090", timeout_seconds=5.0)
        with pytest.raises(AdapterUnavailable):
            adapter.fetch(_make_incident())
    assert call_count[0] == 1


# --------------------------------------------------------------------------- #
# TicketApiAdapter (another HTTP-based adapter; same pattern)
# --------------------------------------------------------------------------- #


def test_ticket_adapter_timeout_is_enforced(monkeypatch) -> None:
    """TicketApiAdapter should also respect the timeout."""
    from ai_employee.rca_agent.tool_adapters import TicketApiAdapter

    def slow_get(*args: Any, **kwargs: Any) -> MagicMock:
        import time

        time.sleep(2.0)
        raise AssertionError("should have timed out")

    monkeypatch.setenv("TICKET_API_URL", "http://ticket.local")
    with patch("httpx.get", side_effect=slow_get):
        adapter = TicketApiAdapter("http://ticket.local", timeout_seconds=0.1)
        with pytest.raises(AdapterUnavailable, match=r"(?i)timeout|unreachable"):
            adapter.fetch(_make_incident())


# --------------------------------------------------------------------------- #
# Resilience function integration (unit-level)
# --------------------------------------------------------------------------- #


def test_fetch_with_resilience_defaults_to_single_shot(monkeypatch) -> None:
    """The helper :func:`resilient_fetch` defaults to max_attempts=1,
    effectively a single-shot call (backward compat)."""
    monkeypatch.delenv("RL_HTTP_RETRY_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("RL_HTTP_RETRY_BACKOFF_SECONDS", raising=False)
    from ai_employee.rca_agent.tool_adapters import resilient_fetch

    call_count = [0]

    def op() -> str:
        call_count[0] += 1
        return "ok"

    result = resilient_fetch(op, timeout_ms=5000)
    assert result == "ok"
    assert call_count[0] == 1


def test_fetch_with_resilience_timeout_aborts_call(monkeypatch) -> None:
    """A slow op is aborted by the timeout and raises an exception."""
    import time

    from ai_employee.rca_agent.http_resilience import _FetchTimeoutError
    from ai_employee.rca_agent.tool_adapters import resilient_fetch

    def slow() -> None:
        time.sleep(2.0)

    with pytest.raises((AdapterUnavailable, _FetchTimeoutError)):
        resilient_fetch(slow, timeout_ms=100)


def test_fetch_with_resilience_retries_customized_via_env(monkeypatch) -> None:
    """Set RL_HTTP_RETRY_MAX_ATTEMPTS=3 via env and verify 3 attempts."""
    monkeypatch.setenv("RL_HTTP_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("RL_HTTP_RETRY_BACKOFF_SECONDS", "0.0")
    from ai_employee.rca_agent.tool_adapters import resilient_fetch

    call_count = [0]

    def op() -> str:
        call_count[0] += 1
        raise RuntimeError("nope")

    with pytest.raises(AdapterUnavailable):
        resilient_fetch(op, timeout_ms=5000)
    assert call_count[0] == 3
