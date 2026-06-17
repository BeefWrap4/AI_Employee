"""Eval Center version comparison.

Given two ``eval_run_id`` records produced by the same eval pipeline, return
the per-metric delta between them so operators can compare pipeline
versions (e.g. ``embed-v2`` vs ``embed-v3``, or ``qwen-turbo`` vs
``qwen-plus``) without re-running anything.

Operates on the unified :class:`UnifiedReport` payload stored in the
``report_json`` column of the ``eval_runs`` table.  Supports both RAG and
RCA eval reports.  Missing metrics are reported as ``null`` so callers
can distinguish a regression from a metric that was not measured.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_employee.common_schemas.eval import UnifiedReport


@dataclass
class MetricDelta:
    metric: str
    a: float | None
    b: float | None
    delta: float | None
    direction: str  # "up" | "down" | "flat" | "unknown"


# Metrics compared for every eval type.
_COMMON_METRICS = ("top1_coverage", "top3_coverage", "evidence_coverage")
_RAG_METRICS = ("refusal_accuracy", "latency_p95_ms")


def load_unified_report(record: dict[str, Any]) -> UnifiedReport:
    """Reconstruct a :class:`UnifiedReport` from a stored eval_runs row.

    Falls back to a default-valued UnifiedReport if the row is missing the
    ``report_json`` payload (e.g. a failed eval that did not produce one).
    """
    raw = record.get("report_json") or {}
    eval_type = (
        raw.get("eval_type")
        or record.get("eval_type")
        or "rag"
    )
    return UnifiedReport(
        eval_type=eval_type,
        total=int(raw.get("total", 0)),
        top1_coverage=float(raw.get("top1_coverage", 0.0)),
        top3_coverage=float(raw.get("top3_coverage", 0.0)),
        evidence_coverage=float(raw.get("evidence_coverage", 0.0)),
        refusal_accuracy=raw.get("refusal_accuracy"),
        latency_p95_ms=raw.get("latency_p95_ms"),
        per_item=list(raw.get("per_item", []) or []),
        raw_report=dict(raw),
    )


def _direction(a: float | None, b: float | None) -> str:
    if a is None or b is None:
        return "unknown"
    if abs(a - b) < 1e-9:
        return "flat"
    return "up" if b > a else "down"


def compare_reports(
    record_a: dict[str, Any],
    record_b: dict[str, Any],
) -> dict[str, Any]:
    """Compute per-metric delta between two stored eval runs."""
    a = load_unified_report(record_a)
    b = load_unified_report(record_b)

    metrics: list[str] = list(_COMMON_METRICS)
    if a.eval_type == "rag" and b.eval_type == "rag":
        metrics.extend(_RAG_METRICS)
    elif a.eval_type != b.eval_type:
        # Cross-type comparison: report coverage metrics only.
        metrics = list(_COMMON_METRICS)

    deltas: list[dict[str, Any]] = []
    for name in metrics:
        a_val = getattr(a, name)
        b_val = getattr(b, name)
        delta = (
            None if a_val is None or b_val is None else round(b_val - a_val, 6)
        )
        deltas.append(
            {
                "metric": name,
                "a": a_val,
                "b": b_val,
                "delta": delta,
                "direction": _direction(a_val, b_val),
            }
        )

    return {
        "a": {
            "eval_run_id": record_a.get("eval_run_id"),
            "eval_type": a.eval_type,
            "template_id": record_a.get("template_id"),
            "created_at": record_a.get("created_at"),
            "completed_at": record_a.get("completed_at"),
            "summary": record_a.get("summary") or {},
        },
        "b": {
            "eval_run_id": record_b.get("eval_run_id"),
            "eval_type": b.eval_type,
            "template_id": record_b.get("template_id"),
            "created_at": record_b.get("created_at"),
            "completed_at": record_b.get("completed_at"),
            "summary": record_b.get("summary") or {},
        },
        "metrics": deltas,
    }


__all__ = ["compare_reports", "load_unified_report", "MetricDelta"]
