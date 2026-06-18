"""Trace / correlation ID helpers.

Provides :func:`new_trace_id`, :func:`new_span_id`, and a context
manager :class:`SpanContext` for propagating trace/span identifiers
through service code without depending on the heavyweight
``opentelemetry-sdk``.

The format mirrors the W3C trace-context layout (``trace_id`` is a
32-hex-char value, ``span_id`` is 16-hex-char) so logs and metrics are
interoperable with downstream collectors that expect that shape.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TRACE_ID: ContextVar[str | None] = ContextVar("observability_trace_id", default=None)
_SPAN_ID: ContextVar[str | None] = ContextVar("observability_span_id", default=None)


def new_trace_id() -> str:
    """Return a fresh 32-hex-char trace id."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """Return a fresh 16-hex-char span id."""
    return secrets.token_hex(8)


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def get_span_id() -> str | None:
    return _SPAN_ID.get()


def set_trace_id(trace_id: str | None) -> None:
    _TRACE_ID.set(trace_id)


def set_span_id(span_id: str | None) -> None:
    _SPAN_ID.set(span_id)


def bind(trace_id: str | None, span_id: str | None) -> tuple[str | None, str | None]:
    """Replace the current trace/span context, returning the previous values."""
    prev_trace = _TRACE_ID.get()
    prev_span = _SPAN_ID.get()
    _TRACE_ID.set(trace_id)
    _SPAN_ID.set(span_id)
    return prev_trace, prev_span


def restore(previous: tuple[str | None, str | None]) -> None:
    """Restore the trace/span context to a previous state."""
    prev_trace, prev_span = previous
    _TRACE_ID.set(prev_trace)
    _SPAN_ID.set(prev_span)


@contextmanager
def span(name: str, *, trace_id: str | None = None) -> Iterator[dict[str, str]]:
    """Open a span, generating trace/span ids and binding them to context.

    Reuses the active trace_id from the current context when no explicit
    ``trace_id`` is supplied, so nested spans share the same trace.  When
    no trace is active, a fresh one is generated.

    Yields a dict with ``trace_id``, ``span_id`` and ``span_name`` keys so
    callers can attach them to log records or metric labels.  Restores
    the previous context on exit.
    """
    trace = trace_id or get_trace_id() or new_trace_id()
    span_id = new_span_id()
    previous = bind(trace, span_id)
    payload = {"trace_id": trace, "span_id": span_id, "span_name": name}
    try:
        yield payload
    finally:
        restore(previous)


def format_traceparent(trace_id: str, span_id: str) -> str:
    """Format a W3C ``traceparent`` header value (00-<trace>-<span>-01)."""
    if len(trace_id) != 32 or len(span_id) != 16:
        raise ValueError("trace_id must be 32 hex chars; span_id must be 16 hex chars")
    return f"00-{trace_id}-{span_id}-01"


def parse_traceparent(header: str) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` header.  Returns ``(trace_id, span_id)`` or
    ``None`` if the header is malformed.
    """
    parts = header.split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, _flags = parts
    if version != "00":
        return None
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    try:
        int(trace_id, 16)
        int(span_id, 16)
    except ValueError:
        return None
    return trace_id, span_id


__all__ = [
    "bind",
    "format_traceparent",
    "get_span_id",
    "get_trace_id",
    "new_span_id",
    "new_trace_id",
    "parse_traceparent",
    "restore",
    "set_span_id",
    "set_trace_id",
    "span",
]
