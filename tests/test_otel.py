"""OpenTelemetry SDK + OTLP export tests (spec 三 spec §可观测 OpenTelemetry)."""
from __future__ import annotations

import pytest

from ai_employee.observability.otel import (
    OTelConfig,
    OTelSpan,
    build_tracer_provider,
    configure_otel,
    parse_otlp_headers,
)


# --------------------------------------------------------------------------- #
# OTelConfig
# --------------------------------------------------------------------------- #


def test_config_defaults_disabled() -> None:
    cfg = OTelConfig()
    assert cfg.enabled is False


def test_config_from_env_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "agent-platform-api")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    cfg = OTelConfig.from_env()
    assert cfg.enabled is True
    assert cfg.service_name == "agent-platform-api"
    assert cfg.endpoint == "http://localhost:4317"
    assert cfg.protocol == "grpc"


def test_config_from_env_disabled_when_no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    cfg = OTelConfig.from_env()
    assert cfg.enabled is False


def test_config_accepts_http_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    cfg = OTelConfig.from_env()
    assert cfg.protocol == "http/protobuf"


# --------------------------------------------------------------------------- #
# parse_otlp_headers
# --------------------------------------------------------------------------- #


def test_parse_otlp_headers_simple() -> None:
    headers = parse_otlp_headers("x-tenant=acme,x-trace-id=abc")
    assert headers == {"x-tenant": "acme", "x-trace-id": "abc"}


def test_parse_otlp_headers_empty_returns_empty() -> None:
    assert parse_otlp_headers("") == {}


def test_parse_otlp_headers_skips_malformed() -> None:
    headers = parse_otlp_headers("k=v,badpair,k2=v2")
    assert headers == {"k": "v", "k2": "v2"}


# --------------------------------------------------------------------------- #
# configure_otel / build_tracer_provider
# --------------------------------------------------------------------------- #


def test_configure_otel_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    result = configure_otel()
    assert result["enabled"] is False
    # Even when disabled, an in-memory provider is installed so spans work.
    assert result["provider"] is not None


def test_configure_otel_enabled_returns_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-svc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    result = configure_otel()
    assert result["enabled"] is True
    assert result["provider"] is not None


def test_build_tracer_provider_no_export_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """When disabled, build an in-memory provider so spans still work in tests."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    cfg = OTelConfig.from_env()
    provider = build_tracer_provider(cfg)
    # In-memory provider is always usable.
    assert provider is not None


# --------------------------------------------------------------------------- #
# OTelSpan helper
# --------------------------------------------------------------------------- #


def test_otel_span_records_name_and_attributes() -> None:
    """OTelSpan wraps a tracer and records name + attributes on exit."""
    monkeypatch_delenv = None
    import os

    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT",):
        os.environ.pop(var, None)
    cfg = OTelConfig()  # disabled → in-memory
    provider = build_tracer_provider(cfg)
    span = OTelSpan(provider=provider, service_name="test")
    with span.start("rca.run", attributes={"run_id": "r1"}) as s:
        s.set_attribute("status", "completed")
    record = span.finish_record()
    assert record["name"] == "rca.run"
    assert record["attributes"]["run_id"] == "r1"
    assert record["attributes"]["status"] == "completed"


def test_otel_span_error_captured() -> None:
    cfg = OTelConfig()
    provider = build_tracer_provider(cfg)
    span = OTelSpan(provider=provider, service_name="test")
    with pytest.raises(ValueError):
        with span.start("bad.op"):
            raise ValueError("boom")
    record = span.finish_record()
    assert record["status"] == "ERROR"
    assert "boom" in record["attributes"].get("error.message", "")
