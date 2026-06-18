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
"""
from __future__ import annotations

import math
import threading
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

    def record_approval(self, wait_seconds: float) -> None:
        self.approvals_total += 1
        self.approval_waits_s.append(wait_seconds)

    def record_model_latency(self, latency_ms: float) -> None:
        self.model_latencies_ms.append(latency_ms)

    def record_tool_latency(self, latency_ms: float) -> None:
        self.tool_latencies_ms.append(latency_ms)

    def record_event(self, *, fallback: bool) -> None:
        self.total_events += 1
        if fallback:
            self.fallback_events += 1

    def record_review(self, *, accepted: bool) -> None:
        self.reports_reviewed += 1
        if accepted:
            self.reports_accepted += 1


_GLOBAL = PlatformMetrics()
_LOCK = threading.Lock()


def metrics() -> PlatformMetrics:
    return _GLOBAL


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    rank = (p / 100) * (len(s) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


def snapshot_dict() -> dict[str, Any]:
    """Return a snapshot dict for the Prometheus text-format renderer."""
    with _LOCK:
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
        "approval_wait_time_p95_s": round(_percentile(m.approval_waits_s, 95), 6),
        "model_latency_p95_ms": round(_percentile(m.model_latencies_ms, 95), 6),
        "tool_latency_p95_ms": round(_percentile(m.tool_latencies_ms, 95), 6),
        "fallback_rate": (
            round(m.fallback_events / m.total_events, 6) if m.total_events else 0.0
        ),
        "report_acceptance_rate": (
            round(m.reports_accepted / m.reports_reviewed, 6)
            if m.reports_reviewed
            else 0.0
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


def reset() -> None:
    """Reset global metrics (test helper)."""
    global _GLOBAL
    with _LOCK:
        _GLOBAL = PlatformMetrics()


__all__ = [
    "PlatformMetrics",
    "metrics",
    "reset",
    "snapshot_dict",
]
