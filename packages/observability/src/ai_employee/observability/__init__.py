"""Shared observability primitives: tracing, metrics, logging.

Importable via ``ai_employee.observability`` once the package is
installed.  See ``correlation`` for trace-id helpers, ``metrics`` for the
Prometheus-compatible text exposition, and ``logging`` for the
trace-id-aware log formatter.
"""

from ai_employee.observability.correlation import (
    bind,
    format_traceparent,
    get_span_id,
    get_trace_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    restore,
    set_span_id,
    set_trace_id,
    span,
)
from ai_employee.observability.langfuse_emitter import (
    LangfuseEmitter,
    build_langfuse_emitter,
)
from ai_employee.observability.logging import (
    configure_logging,
    is_configured,
    log_event,
)
from ai_employee.observability.metrics import (
    MetricRegistry,
    configure_default_registry,
    get_default_registry,
    render_prometheus_text,
    reset_default_registry,
)
from ai_employee.observability.otel import (
    OTelConfig,
    OTelSpan,
    build_tracer_provider,
    configure_otel,
    parse_otlp_headers,
)

__all__ = [
    "LangfuseEmitter",
    "MetricRegistry",
    "OTelConfig",
    "OTelSpan",
    "bind",
    "build_langfuse_emitter",
    "build_tracer_provider",
    "configure_default_registry",
    "configure_logging",
    "configure_otel",
    "format_traceparent",
    "get_default_registry",
    "get_span_id",
    "get_trace_id",
    "is_configured",
    "log_event",
    "new_span_id",
    "new_trace_id",
    "parse_otlp_headers",
    "parse_traceparent",
    "render_prometheus_text",
    "reset_default_registry",
    "restore",
    "set_span_id",
    "set_trace_id",
    "span",
]
