"""R25-O: observability metrics wiring tests."""

from __future__ import annotations

from ai_employee.common_schemas.metrics_bridge import (
    metrics,
    platform_metrics,
    snapshot_dict,
    to_prometheus_text,
)


def test_metrics_singleton_returns_same_instance() -> None:
    assert platform_metrics() is metrics()


def test_record_model_latency_appears_in_snapshot() -> None:
    pm = platform_metrics()
    initial_count = len(pm.model_latencies_ms)
    pm.record_model_latency(123.4)
    assert len(pm.model_latencies_ms) == initial_count + 1
    snap = snapshot_dict()
    assert "model_latency_p95_ms" in snap


def test_record_tool_latency_appears_in_snapshot() -> None:
    pm = platform_metrics()
    pm.record_tool_latency(50.0)
    snap = snapshot_dict()
    assert "tool_latency_p95_ms" in snap


def test_record_event_fallback_updates_fallback_rate() -> None:
    pm = platform_metrics()
    initial_fallback = pm.fallback_events
    initial_total = pm.total_events
    pm.record_event(fallback=True)
    pm.record_event(fallback=False)
    assert pm.fallback_events == initial_fallback + 1
    assert pm.total_events == initial_total + 2
    snap = snapshot_dict()
    assert "fallback_rate" in snap


def test_record_run_updates_agent_run_success_rate() -> None:
    pm = platform_metrics()
    pm.record_run(succeeded=True)
    pm.record_run(succeeded=False)
    snap = snapshot_dict()
    assert snap["agent_run_success_rate"] != 0.0


def test_record_review_updates_report_acceptance_rate() -> None:
    pm = platform_metrics()
    pm.record_review(accepted=True)
    pm.record_review(accepted=False)
    snap = snapshot_dict()
    assert snap["report_acceptance_rate"] != 0.0


def test_record_approval_updates_approval_wait() -> None:
    pm = platform_metrics()
    pm.record_approval(2.5)
    snap = snapshot_dict()
    assert "approval_wait_time_p95_s" in snap


def test_prometheus_text_contains_seven_indicators() -> None:
    text = to_prometheus_text()
    for name in (
        "agent_run_success_rate",
        "approval_wait_time_p95_s",
        "report_acceptance_rate",
        "model_latency_p95_ms",
        "tool_latency_p95_ms",
        "fallback_rate",
        "tool_call_success_rate",
    ):
        assert f"platform_{name}" in text, f"missing indicator {name!r} in:\n{text}"


def test_snapshot_dict_has_seven_keys() -> None:
    snap = snapshot_dict()
    expected = {
        "agent_run_success_rate",
        "approval_wait_time_p95_s",
        "report_acceptance_rate",
        "model_latency_p95_ms",
        "tool_latency_p95_ms",
        "fallback_rate",
        "tool_call_success_rate",
    }
    assert expected.issubset(set(snap.keys()))


def test_prometheus_text_with_explicit_snapshot() -> None:
    snap = {
        "agent_run_success_rate": 0.95,
        "approval_wait_time_p95_s": 1.0,
        "report_acceptance_rate": 0.8,
        "model_latency_p95_ms": 200.0,
        "tool_latency_p95_ms": 50.0,
        "fallback_rate": 0.02,
        "tool_call_success_rate": 0.99,
    }
    text = to_prometheus_text(snap)
    assert "platform_agent_run_success_rate 0.95" in text
    assert "platform_model_latency_p95_ms 200.0" in text
