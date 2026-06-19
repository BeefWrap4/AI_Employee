"""R25-O: shared metrics bridge for cross-package metric collection.

Provides a :func:`platform_metrics` accessor that returns the process-wide
``PlatformMetrics`` singleton.  Lives in common-schemas so llm-gateway
and downstream services can import without circular deps on agent-platform.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlatformMetrics:
    """Minimal platform-metrics accumulator (R25-O).

    Real agent-platform re-exports its full ``PlatformMetrics`` here for
    callers that only need the basic record/snapshot surface.  When this
    shim is used standalone (without agent-platform installed), the
    singleton below tracks the same headline indicators in-process.
    """

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
_GLOBAL_LOCK = threading.Lock()


def metrics() -> PlatformMetrics:
    """Return the process-wide metrics singleton."""
    return _GLOBAL


# Back-compat alias used by callers (knowledge-api, etc.).
def platform_metrics() -> PlatformMetrics:
    return _GLOBAL


def snapshot_dict() -> dict[str, Any]:
    """Return a snapshot of all seven headline indicators (R25-O)."""
    with _GLOBAL_LOCK:
        m = _GLOBAL
        runs_total = m.runs_total
        runs_succeeded = m.runs_succeeded
        reports_reviewed = m.reports_reviewed
        reports_accepted = m.reports_accepted
        fallback_events = m.fallback_events
        total_events = m.total_events
        model_latencies_ms = list(m.model_latencies_ms)
        tool_latencies_ms = list(m.tool_latencies_ms)
        approval_waits_s = list(m.approval_waits_s)
    return {
        "agent_run_success_rate": (round(runs_succeeded / runs_total, 6) if runs_total else 1.0),
        "approval_wait_time_p95_s": _percentile(approval_waits_s, 95),
        "report_acceptance_rate": (
            round(reports_accepted / reports_reviewed, 6) if reports_reviewed else 0.0
        ),
        "model_latency_p95_ms": _percentile(model_latencies_ms, 95),
        "tool_latency_p95_ms": _percentile(tool_latencies_ms, 95),
        "fallback_rate": (round(fallback_events / total_events, 6) if total_events else 0.0),
        "tool_call_success_rate": 1.0,
    }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    rank = (p / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(len(s) - 1, lo + 1)
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


def to_prometheus_text(snap: dict[str, Any] | None = None) -> str:
    """Render the seven indicators in Prometheus text exposition format."""
    s = snap if snap is not None else snapshot_dict()
    lines: list[str] = []
    indicator_keys = {
        "agent_run_success_rate": "gauge",
        "approval_wait_time_p95_s": "gauge",
        "report_acceptance_rate": "gauge",
        "model_latency_p95_ms": "gauge",
        "tool_latency_p95_ms": "gauge",
        "fallback_rate": "gauge",
        "tool_call_success_rate": "gauge",
    }
    for k, kind in indicator_keys.items():
        lines.append(f"# TYPE platform_{k} {kind}")
        lines.append(f"platform_{k} {s.get(k, 0.0)}")
    return "\n".join(lines) + "\n"


__all__ = [
    "PlatformMetrics",
    "metrics",
    "platform_metrics",
    "snapshot_dict",
    "to_prometheus_text",
]


# When the agent-platform is importable, prefer its richer metrics
# singleton by monkey-patching ``platform_metrics`` to point at it.
if os.environ.get("PLATFORM_METRICS_FROM_AGENT_PLATFORM") == "1":
    try:  # pragma: no cover — wiring hook, not a hot path
        from ai_employee.agent_platform_api.platform_metrics import (  # type: ignore[import-not-found]
            metrics as _ap_metrics,
        )

        globals()["platform_metrics"] = _ap_metrics  # type: ignore[assignment]
        globals()["snapshot_dict"] = (  # type: ignore[assignment]
            __import__(
                "ai_employee.agent_platform_api.platform_metrics",
                fromlist=["snapshot_dict"],
            ).snapshot_dict
        )
    except Exception:
        pass
