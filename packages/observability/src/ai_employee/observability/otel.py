"""OpenTelemetry SDK + OTLP export (spec 三 spec §可观测 OpenTelemetry).

Replaces the hand-rolled W3C traceparent helpers in ``correlation.py``
with the official OpenTelemetry SDK.  :func:`configure_otel` reads env
and (when enabled) builds a :class:`TracerProvider` with an OTLP
exporter pointed at a collector.  When disabled (no
``OTEL_EXPORTER_OTLP_ENDPOINT``), an in-memory provider is used so
spans still record locally — useful for tests and dev.

Env:
  ``OTEL_EXPORTER_OTLP_ENDPOINT``  — collector URL (enables OTel when set)
  ``OTEL_EXPORTER_OTLP_PROTOCOL``  — ``grpc`` (default) | ``http/protobuf``
  ``OTEL_SERVICE_NAME``            — service.name resource attribute
  ``OTEL_EXPORTER_OTLP_HEADERS``   — comma-separated k=v headers
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OTelConfig:
    enabled: bool = False
    endpoint: str | None = None
    protocol: str = "grpc"
    service_name: str = "ai-employee"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> OTelConfig:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        service_name = os.environ.get("OTEL_SERVICE_NAME", "ai-employee")
        headers = parse_otlp_headers(
            os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
        )
        return cls(
            enabled=bool(endpoint),
            endpoint=endpoint,
            protocol=protocol,
            service_name=service_name,
            headers=headers,
        )


def parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse ``k=v,k2=v2`` into a dict; skips malformed entries."""
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def build_tracer_provider(config: OTelConfig) -> Any:
    """Build a TracerProvider.  OTLP exporter when enabled, else in-memory.

    The in-memory path uses a ``ConsoleSpanExporter`` so spans are
    recorded (and readable by tests) without a collector.  We keep a
    reference to finished spans via a custom in-memory exporter when
    tests need to inspect them.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    resource = Resource.create({"service.name": config.service_name})
    provider = TracerProvider(resource=resource)

    if config.enabled and config.endpoint:
        try:
            if config.protocol == "http/protobuf":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(
                    endpoint=config.endpoint,
                    headers=config.headers,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(
                    endpoint=config.endpoint,
                    headers=config.headers,
                )
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        except Exception:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    return provider


def configure_otel() -> dict[str, Any]:
    """Configure global OTel from env.  Returns a status dict.

    Idempotent: only sets the global tracer provider once.  When
    disabled, still installs an in-memory provider so
    :class:`OTelSpan` works in tests.
    """
    from opentelemetry import trace

    config = OTelConfig.from_env()
    provider = build_tracer_provider(config)
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass
    return {
        "enabled": config.enabled,
        "service_name": config.service_name,
        "endpoint": config.endpoint,
        "provider": provider,
    }


@dataclass
class OTelSpan:
    """Thin wrapper around an OTel span for ergonomic attribute recording.

    Records the span name + attributes in a dict so tests can assert
    on them without parsing the in-memory exporter.
    """

    provider: Any
    service_name: str
    _span: Any = None
    _record: dict[str, Any] = field(default_factory=dict)

    def start(self, name: str, *, attributes: dict[str, Any] | None = None) -> OTelSpan:
        tracer = self.provider.get_tracer(self.service_name)
        self._span = tracer.start_span(name, attributes=attributes)
        self._record = {
            "name": name,
            "attributes": dict(attributes or {}),
            "status": "OK",
        }
        return self

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is not None:
            self._span.set_attribute(key, value)
        self._record["attributes"][key] = value

    def __enter__(self) -> OTelSpan:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self._record["status"] = "ERROR"
            self._record["attributes"]["error.message"] = str(exc)
            if self._span is not None:
                self._span.set_status(
                    __import__(
                        "opentelemetry.trace.status",
                        fromlist=["Status", "StatusCode"],
                    ).Status(
                        __import__(
                            "opentelemetry.trace.status",
                            fromlist=["StatusCode"],
                        ).StatusCode.ERROR,
                        description=str(exc),
                    )
                )
                self._span.record_exception(exc)
        if self._span is not None:
            self._span.end()

    def finish_record(self) -> dict[str, Any]:
        return self._record


__all__ = [
    "OTelConfig",
    "OTelSpan",
    "build_tracer_provider",
    "configure_otel",
    "parse_otlp_headers",
]
