"""Observability package tests — correlation, metrics, logging."""

from __future__ import annotations

import io
import logging

import pytest
from ai_employee.observability import (
    MetricRegistry,
    bind,
    configure_logging,
    format_traceparent,
    get_span_id,
    get_trace_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    render_prometheus_text,
    reset_default_registry,
    restore,
    set_span_id,
    set_trace_id,
    span,
)


def test_new_trace_id_is_32_hex() -> None:
    tid = new_trace_id()
    assert len(tid) == 32
    int(tid, 16)


def test_new_span_id_is_16_hex() -> None:
    sid = new_span_id()
    assert len(sid) == 16
    int(sid, 16)


def test_span_context_binds_and_restores() -> None:
    set_trace_id(None)
    set_span_id(None)
    with span("op") as payload:
        assert get_trace_id() == payload["trace_id"]
        assert get_span_id() == payload["span_id"]
        assert payload["span_name"] == "op"
    assert get_trace_id() is None
    assert get_span_id() is None


def test_nested_spans_restore_outer_context() -> None:
    set_trace_id(None)
    with span("outer") as outer:
        with span("inner") as inner:
            assert inner["trace_id"] == outer["trace_id"]
            assert inner["span_id"] != outer["span_id"]
            assert get_trace_id() == inner["trace_id"]
        assert get_trace_id() == outer["trace_id"]
    assert get_trace_id() is None


def test_bind_and_restore_roundtrip() -> None:
    set_trace_id(None)
    bind("a" * 32, "b" * 16)
    assert get_trace_id() == "a" * 32
    assert get_span_id() == "b" * 16
    restore((None, None))
    assert get_trace_id() is None


def test_traceparent_format_and_parse() -> None:
    trace = new_trace_id()
    span_id = new_span_id()
    header = format_traceparent(trace, span_id)
    assert header.startswith("00-")
    parsed = parse_traceparent(header)
    assert parsed == (trace, span_id)


def test_parse_traceparent_rejects_malformed() -> None:
    assert parse_traceparent("not-a-traceparent") is None
    assert parse_traceparent("01-aa-bb-01") is None
    assert parse_traceparent("00-" + "z" * 32 + "-bb-01") is None


def test_format_traceparent_rejects_wrong_lengths() -> None:
    with pytest.raises(ValueError):
        format_traceparent("tooshort", "bb")


def test_metrics_counter_gauge_histogram(tmp_path) -> None:
    reset_default_registry()
    reg = MetricRegistry()
    counter = reg.register_counter("requests_total", "Total requests")
    gauge = reg.register_gauge("queue_depth", "Queue depth")
    hist = reg.register_histogram("latency_seconds", "Latency", buckets=(0.1, 0.5, 1.0))
    counter.inc(amount=2)
    counter.inc(amount=3)
    gauge.set(7)
    hist.observe(0.05)
    hist.observe(0.3)
    hist.observe(0.9)
    text = render_prometheus_text(reg)
    assert "# TYPE requests_total counter" in text
    assert "requests_total 5" in text
    assert "queue_depth 7" in text
    assert 'latency_seconds_bucket{le="0.1"} 1' in text
    assert 'latency_seconds_bucket{le="+Inf"} 3' in text
    assert "latency_seconds_count 3" in text


def test_metrics_counter_rejects_negative() -> None:
    reg = MetricRegistry()
    counter = reg.register_counter("x_total", "x")
    with pytest.raises(ValueError):
        counter.inc(amount=-1)


def test_metrics_duplicate_registration_raises() -> None:
    reg = MetricRegistry()
    reg.register_counter("dup_total", "x")
    with pytest.raises(ValueError):
        reg.register_counter("dup_total", "y")


def test_logging_includes_trace_context() -> None:
    set_trace_id(None)
    configure_logging()
    logger = logging.getLogger("test_observe")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter("%(levelname)s trace=%(trace_id)s span=%(span_id)s %(message)s"),
    )
    handler.addFilter(
        __import__(
            "ai_employee.observability.logging", fromlist=["_ContextFilter"]
        )._ContextFilter()
    )
    logger.handlers = [handler]
    logger.propagate = False
    with span("work"):
        logger.info("hello")
    output = stream.getvalue()
    assert "trace=" in output
    assert "span=" in output
    assert "hello" in output
