"""Logging helper that attaches trace_id / span_id to every record.

Wraps the standard :mod:`logging` module so existing log statements pick
up the active trace context without manual changes.  Use
:func:`configure_logging` once at process start; afterwards
``logger.info("...")`` automatically emits ``trace_id=...`` /
``span_id=...`` keys.
"""
from __future__ import annotations

import logging
from typing import Any

from ai_employee.observability.correlation import get_span_id, get_trace_id


class _ContextFilter(logging.Filter):
    """Inject trace/span ids from the current context into every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.trace_id = get_trace_id() or "-"
        record.span_id = get_span_id() or "-"
        return True


_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Install a single stream handler with the trace-id filter.

    Idempotent: subsequent calls just adjust the level.
    """
    global _CONFIGURED
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [trace=%(trace_id)s span=%(span_id)s] %(name)s: %(message)s",
        )
    )
    handler.addFilter(_ContextFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def is_configured() -> bool:
    return _CONFIGURED


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Emit ``logger.log(level, message, extra=fields)`` with trace context."""
    extra = {"trace_id": get_trace_id() or "-", "span_id": get_span_id() or "-"}
    extra.update(fields)
    logger.log(level, message, extra=extra)


__all__ = ["configure_logging", "is_configured", "log_event"]
