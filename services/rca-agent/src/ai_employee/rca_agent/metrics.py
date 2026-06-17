"""Operational metrics for the RCA agent (spec §4.6).

Aggregates per-run counters into four headline metrics:

* **Tool Call Success Rate** = 1 - tool_failures / tool_attempts
* **Human Acceptance Rate** = accepted / reviewed
* **Alert Compression Ratio** = incident_count / alarm_count_total
  (1.0 means one incident per alarm; higher = better compression)
* **Report Gen Time (avg)** = total_gen_seconds / report_count

These counters are populated by the runtime + app layer via
:meth:`RcaStore.record_tool_call`, :meth:`RcaStore.record_review`, and
:meth:`RcaStore.record_report_generated`. The :func:`compute_metrics`
helper turns them into a JSON-friendly dict for the eval center.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RcaOperationalMetrics:
    tool_call_success_rate: float
    human_acceptance_rate: float
    alert_compression_ratio: float
    report_gen_seconds_avg: float
    raw: dict[str, float | int] = field(default_factory=dict)


def compute_metrics(store) -> RcaOperationalMetrics:
    """Compute the four operational metrics from a runtime store.

    Accepts any object exposing the same attributes as
    :class:`ai_employee.rca_agent.runtime.RcaStore` (kept loose to
    avoid an import cycle).
    """
    tool_attempts = max(1, getattr(store, "tool_call_attempts", 0))
    tool_failures = getattr(store, "tool_call_failures", 0)
    tool_success_rate = max(0.0, 1.0 - (tool_failures / tool_attempts))

    reviewed = max(1, getattr(store, "reviewed_reports", 0))
    accepted = getattr(store, "accepted_reports", 0)
    human_acceptance_rate = accepted / reviewed

    alarm_total = max(1, getattr(store, "alarm_count_total", 0))
    incident_alarm_total = max(1, getattr(store, "incident_alarm_total", 0))
    alert_compression_ratio = incident_alarm_total / alarm_total

    report_count = max(1, getattr(store, "report_gen_count", 0))
    gen_total = getattr(store, "report_gen_seconds_total", 0.0)
    report_gen_avg = gen_total / report_count

    raw = {
        "tool_call_attempts": tool_attempts,
        "tool_call_failures": tool_failures,
        "reviewed_reports": reviewed,
        "accepted_reports": accepted,
        "alarm_count_total": alarm_total,
        "incident_alarm_total": incident_alarm_total,
        "report_gen_seconds_total": gen_total,
        "report_gen_count": report_count,
    }
    return RcaOperationalMetrics(
        tool_call_success_rate=round(tool_success_rate, 6),
        human_acceptance_rate=round(human_acceptance_rate, 6),
        alert_compression_ratio=round(alert_compression_ratio, 6),
        report_gen_seconds_avg=round(report_gen_avg, 6),
        raw=raw,
    )


__all__ = ["RcaOperationalMetrics", "compute_metrics"]
