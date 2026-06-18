"""Platform metrics endpoint + helpers."""
from __future__ import annotations

from ai_employee.agent_platform_api import platform_metrics
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.platform_metrics import (
    metrics as metrics_obj,
)
from ai_employee.agent_platform_api.platform_metrics import (
    reset,
    snapshot_dict,
    snapshot_timeseries,
)
from fastapi.testclient import TestClient


def test_snapshot_dict_empty() -> None:
    reset()
    snap = snapshot_dict()
    assert snap["agent_run_success_rate"] == 1.0
    assert snap["approval_wait_time_p95_s"] == 0.0
    assert snap["fallback_rate"] == 0.0
    assert snap["raw"]["runs_total"] == 0


def test_snapshot_dict_records_runs_and_p95() -> None:
    reset()
    m = platform_metrics.metrics()
    m.record_run(succeeded=True)
    m.record_run(succeeded=True)
    m.record_run(succeeded=False)
    m.record_approval(2.0)
    m.record_approval(10.0)
    m.record_tool_latency(50)
    m.record_tool_latency(150)
    m.record_model_latency(200)
    m.record_model_latency(1200)
    snap = snapshot_dict()
    assert snap["agent_run_success_rate"] == pytest.approx(2 / 3)
    # p95 with linear interpolation between 2 sorted samples:
    # 95% rank = 0.95; result = lo + (hi - lo) * 0.95.
    assert snap["approval_wait_time_p95_s"] == pytest.approx(2 + 8 * 0.95)
    assert snap["tool_latency_p95_ms"] == pytest.approx(50 + 100 * 0.95)
    assert snap["model_latency_p95_ms"] == pytest.approx(200 + 1000 * 0.95)
    assert snap["raw"]["runs_total"] == 3


def test_platform_metrics_endpoint_returns_dict() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/metrics/platform")
    assert resp.status_code == 200
    body = resp.json()
    assert "agent_run_success_rate" in body
    assert "approval_wait_time_p95_s" in body
    assert "model_latency_p95_ms" in body
    assert "tool_latency_p95_ms" in body
    assert "fallback_rate" in body
    assert "report_acceptance_rate" in body


def test_fallback_rate_computed() -> None:
    reset()
    m = platform_metrics.metrics()
    m.record_event(fallback=False)
    m.record_event(fallback=True)
    m.record_event(fallback=False)
    snap = snapshot_dict()
    assert snap["fallback_rate"] == pytest.approx(1 / 3)


def test_snapshot_timeseries_empty_after_reset() -> None:
    reset()
    ts = snapshot_timeseries()
    assert ts["samples"] == []
    assert ts["maxlen"] > 0


def test_snapshot_timeseries_records_after_runs() -> None:
    reset()
    m = metrics_obj()
    m.record_run(succeeded=True)
    m.record_approval(2.5)
    m.record_model_latency(180)
    m.record_tool_latency(60)
    ts = snapshot_timeseries()
    assert len(ts["samples"]) >= 1
    sample = ts["samples"][-1]
    assert sample["agent_run_success_rate"] == 1.0
    assert sample["model_latency_p95_ms"] == 180.0
    assert sample["tool_latency_p95_ms"] == 60.0
    assert sample["approval_wait_time_p95_s"] == 2.5
    assert "timestamp" in sample


def test_snapshot_timeseries_caps_to_maxlen() -> None:
    reset()
    m = metrics_obj()
    # Drive more samples than maxlen to confirm FIFO eviction.
    maxlen = snapshot_timeseries()["maxlen"]
    for _ in range(maxlen + 25):
        m.record_run(succeeded=True)
    ts = snapshot_timeseries()
    assert len(ts["samples"]) == maxlen


def test_platform_timeseries_endpoint_returns_samples() -> None:
    reset()
    client = TestClient(create_app())
    resp = client.get("/api/v1/metrics/platform/timeseries")
    assert resp.status_code == 200
    body = resp.json()
    assert "samples" in body
    assert "maxlen" in body
    assert isinstance(body["samples"], list)


import pytest  # noqa: E402
