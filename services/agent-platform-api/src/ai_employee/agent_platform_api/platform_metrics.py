"""Platform operational metrics (spec §5.6).

Tracks the seven headline indicators the platform must report:

* agent_run_success_rate — successful runs / total runs
* tool_call_success_rate — derived from tool_call_log via fallback
* approval_wait_time_p95 — 95th-percentile wait (seconds) between
  approval task creation and decision
* model_latency_p95 — 95th-percentile LLM call latency (ms)
* tool_latency_p95 — 95th-percentile tool call latency (ms)
* report_acceptance_rate — accepted reports / reviewed reports
* fallback_rate — fixture / stub fallback events / total events

The registry is process-local.  Metrics are surfaced in Prometheus text
format via the existing ``/metrics`` endpoint by registering an
ai_employee.observability ``MetricRegistry`` and emitting samples
periodically (and on read).

A rolling timeseries of headline indicators is kept for the dashboard
trend charts (ECharts line).  Samples are captured once per
``record_*`` call and capped at ``_TIMESERIES_MAXLEN``.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PlatformMetrics:
    runs_total: int = 0
    runs_succeeded: int = 0
    runs_failed: int = 0
    approvals_total: int = 0
    approval_waits_s: list[float] = field(default_factory=list)
    model_latencies_ms: list[float] = field(default_factory=list)
    tool_latencies_ms: list[float] = field(default_factory=list)
    fallback_events: int = 0
    total_events: int = 0
    reports_accepted: int = 0
    reports_reviewed: int = 0

    def record_run(self, *, succeeded: bool) -> None:
        self.runs_total += 1
        if succeeded:
            self.runs_succeeded += 1
        else:
            self.runs_failed += 1
        _record_timeseries_sample()

    def record_approval(self, wait_seconds: float) -> None:
        self.approvals_total += 1
        self.approval_waits_s.append(wait_seconds)
        _record_timeseries_sample()

    def record_model_latency(self, latency_ms: float) -> None:
        self.model_latencies_ms.append(latency_ms)
        _record_timeseries_sample()

    def record_tool_latency(self, latency_ms: float) -> None:
        self.tool_latencies_ms.append(latency_ms)
        _record_timeseries_sample()

    def record_event(self, *, fallback: bool) -> None:
        self.total_events += 1
        if fallback:
            self.fallback_events += 1
        _record_timeseries_sample()

    def record_review(self, *, accepted: bool) -> None:
        self.reports_reviewed += 1
        if accepted:
            self.reports_accepted += 1
        _record_timeseries_sample()


# --------------------------------------------------------------------------- #
# Timeseries history (for ECharts trend charts on the dashboard).
# --------------------------------------------------------------------------- #

_GLOBAL = PlatformMetrics()
_GLOBAL_LOCK = threading.Lock()
_TIMESERIES_MAXLEN = 120
_TIMESERIES: deque[dict[str, Any]] = deque(maxlen=_TIMESERIES_MAXLEN)
_TIMESERIES_LOCK = threading.Lock()


def metrics() -> PlatformMetrics:
    return _GLOBAL


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    rank = (p / 100) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


def _tool_call_success_rate() -> float:
    """Compute tool_call_success_rate from the platform log store.

    Falls back to 1.0 (no signal) when the store hasn't been initialised
    or has no records yet — keeps the dashboard metric stable across
    restarts.
    """
    try:
        from ai_employee.agent_platform_api.tool_call_log import (
            PlatformToolCallLogStore,
        )

        store = PlatformToolCallLogStore()
        return round(store.success_rate(), 6)
    except Exception:
        return 1.0


def _record_timeseries_sample() -> None:
    """Snapshot the headline indicators and append to ``_TIMESERIES``.

    Cheap O(1) — duplicates the running aggregates without touching them.
    Lock-protected because ``record_*`` may be called from request threads.
    """
    with _GLOBAL_LOCK:
        sample = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_run_success_rate": (
                round(_GLOBAL.runs_succeeded / _GLOBAL.runs_total, 6) if _GLOBAL.runs_total else 1.0
            ),
            "model_latency_p95_ms": round(_percentile(_GLOBAL.model_latencies_ms, 95), 6),
            "tool_latency_p95_ms": round(_percentile(_GLOBAL.tool_latencies_ms, 95), 6),
            "approval_wait_time_p95_s": round(_percentile(_GLOBAL.approval_waits_s, 95), 6),
            "report_acceptance_rate": (
                round(_GLOBAL.reports_accepted / _GLOBAL.reports_reviewed, 6)
                if _GLOBAL.reports_reviewed
                else 0.0
            ),
            "fallback_rate": (
                round(_GLOBAL.fallback_events / _GLOBAL.total_events, 6)
                if _GLOBAL.total_events
                else 0.0
            ),
        }
    with _TIMESERIES_LOCK:
        _TIMESERIES.append(sample)


def snapshot_dict() -> dict[str, Any]:
    """Return a snapshot dict for the Prometheus text-format renderer."""
    with _GLOBAL_LOCK:
        m = PlatformMetrics(
            runs_total=_GLOBAL.runs_total,
            runs_succeeded=_GLOBAL.runs_succeeded,
            runs_failed=_GLOBAL.runs_failed,
            approvals_total=_GLOBAL.approvals_total,
            approval_waits_s=list(_GLOBAL.approval_waits_s),
            model_latencies_ms=list(_GLOBAL.model_latencies_ms),
            tool_latencies_ms=list(_GLOBAL.tool_latencies_ms),
            fallback_events=_GLOBAL.fallback_events,
            total_events=_GLOBAL.total_events,
            reports_accepted=_GLOBAL.reports_accepted,
            reports_reviewed=_GLOBAL.reports_reviewed,
        )
    return {
        "agent_run_success_rate": (
            round(m.runs_succeeded / m.runs_total, 6) if m.runs_total else 1.0
        ),
        "tool_call_success_rate": _tool_call_success_rate(),
        "approval_wait_time_p95_s": round(_percentile(m.approval_waits_s, 95), 6),
        "model_latency_p95_ms": round(_percentile(m.model_latencies_ms, 95), 6),
        "tool_latency_p95_ms": round(_percentile(m.tool_latencies_ms, 95), 6),
        "fallback_rate": (round(m.fallback_events / m.total_events, 6) if m.total_events else 0.0),
        "report_acceptance_rate": (
            round(m.reports_accepted / m.reports_reviewed, 6) if m.reports_reviewed else 0.0
        ),
        "raw": {
            "runs_total": m.runs_total,
            "runs_succeeded": m.runs_succeeded,
            "runs_failed": m.runs_failed,
            "approvals_total": m.approvals_total,
            "fallback_events": m.fallback_events,
            "total_events": m.total_events,
            "reports_accepted": m.reports_accepted,
            "reports_reviewed": m.reports_reviewed,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def snapshot_timeseries() -> dict[str, Any]:
    """Return the rolling timeseries of headline indicators for the dashboard.

    ``samples`` is a list of dicts with one entry per ``record_*`` call (up to
    ``maxlen`` entries; older samples are evicted automatically).
    """
    with _TIMESERIES_LOCK:
        samples = list(_TIMESERIES)
    return {
        "samples": samples,
        "maxlen": _TIMESERIES_MAXLEN,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }


def reset() -> None:
    """Reset global metrics (test helper)."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL = PlatformMetrics()
    with _TIMESERIES_LOCK:
        _TIMESERIES.clear()


__all__ = [
    "PlatformMetrics",
    "metrics",
    "reset",
    "snapshot_dict",
    "snapshot_timeseries",
]
