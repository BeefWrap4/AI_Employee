"""Platform observability metrics endpoint + runtime wiring tests
(spec §5.6 7 指标).

Drives the four remaining gaps:

* ``agent_run_success_rate`` — recorded on every run completion/failure.
* ``model_latency_p95`` — recorded around LLM calls.
* ``tool_latency_p95`` — recorded around tool invocations.
* ``fallback_rate`` — recorded when an alternate strategy is used.

Surfaced via ``GET /api/v1/metrics/platform``.
"""
from __future__ import annotations

import pytest
from ai_employee.agent_platform_api import platform_metrics
from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    platform_metrics.reset()


def test_metrics_endpoint_returns_seven_indicators() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/metrics/platform")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "agent_run_success_rate",
        "tool_call_success_rate",
        "report_acceptance_rate",
        "model_latency_p95_ms",
        "tool_latency_p95_ms",
        "approval_wait_time_p95_s",
        "fallback_rate",
    ):
        assert key in body, f"missing {key}"


def test_record_run_updates_success_rate() -> None:
    m = platform_metrics.metrics()
    m.record_run(succeeded=True)
    m.record_run(succeeded=True)
    m.record_run(succeeded=False)
    snap = platform_metrics.snapshot_dict()
    assert snap["agent_run_success_rate"] == pytest.approx(2 / 3, abs=1e-6)


def test_record_model_latency_p95() -> None:
    m = platform_metrics.metrics()
    for i in range(1, 101):
        m.record_model_latency(float(i))
    snap = platform_metrics.snapshot_dict()
    assert 90 <= snap["model_latency_p95_ms"] <= 100


def test_record_tool_latency_p95() -> None:
    m = platform_metrics.metrics()
    for i in range(1, 101):
        m.record_tool_latency(float(i))
    snap = platform_metrics.snapshot_dict()
    assert 90 <= snap["tool_latency_p95_ms"] <= 100


def test_record_fallback_event() -> None:
    m = platform_metrics.metrics()
    m.record_event(fallback=False)
    m.record_event(fallback=False)
    m.record_event(fallback=True)
    snap = platform_metrics.snapshot_dict()
    assert snap["fallback_rate"] == pytest.approx(1 / 3, abs=1e-6)


def test_metrics_endpoint_after_recording() -> None:
    m = platform_metrics.metrics()
    m.record_run(succeeded=True)
    m.record_model_latency(50.0)
    m.record_tool_latency(20.0)
    m.record_event(fallback=False)

    client = TestClient(create_app())
    resp = client.get("/api/v1/metrics/platform")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_run_success_rate"] == 1.0
    assert body["model_latency_p95_ms"] == 50.0
    assert body["tool_latency_p95_ms"] == 20.0
    assert body["fallback_rate"] == 0.0


def test_timeseries_history_returns_recent_samples() -> None:
    m = platform_metrics.metrics()
    for _ in range(3):
        m.record_run(succeeded=True)
    ts = platform_metrics.snapshot_timeseries()
    assert "samples" in ts
    assert len(ts["samples"]) >= 3
    assert "agent_run_success_rate" in ts["samples"][-1]


def test_reset_zeroes_everything() -> None:
    m = platform_metrics.metrics()
    m.record_run(succeeded=True)
    m.record_model_latency(50.0)
    platform_metrics.reset()
    snap = platform_metrics.snapshot_dict()
    assert snap["agent_run_success_rate"] == 1.0  # default when total=0
    assert snap["model_latency_p95_ms"] == 0.0
