"""Unified evaluation report model shared across RAG and RCA eval centers.

This module is intentionally dependency-free at runtime: the RAG adapter is
duck-typed against :class:`ai_employee.eval.metrics.EvalMetrics` (imported only
under ``TYPE_CHECKING``) so ``common-schemas`` never imports the eval-service
package at runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from ai_employee.eval.metrics import EvalMetrics


@dataclass
class UnifiedReport:
    """Platform-unified evaluation report (spec §7.4).

    ``refusal_accuracy`` and ``latency_p95_ms`` are RAG-only metrics and are
    ``None`` for RCA evaluations.
    """

    eval_type: str
    total: int
    top1_coverage: float
    top3_coverage: float
    evidence_coverage: float
    refusal_accuracy: float | None
    latency_p95_ms: float | None
    per_item: list[dict[str, Any]] = field(default_factory=list)
    raw_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict (deep copy of nested structures)."""
        return asdict(self)


def to_unified_rag(metrics: "EvalMetrics", report: dict[str, Any]) -> UnifiedReport:
    """Adapt a RAG eval ``EvalMetrics`` + ``build_report`` dict into UnifiedReport.

    - ``top1_coverage`` / ``top3_coverage`` map to Top-1 / Top-3 hit rates.
    - ``evidence_coverage`` maps to RAG citation coverage.
    - ``refusal_accuracy`` and ``latency_p95_ms`` are RAG-specific.
    """
    hit_rates = getattr(metrics, "hit_rates", {}) or {}
    return UnifiedReport(
        eval_type="rag",
        total=int(getattr(metrics, "total", 0)),
        top1_coverage=float(hit_rates.get(1, 0.0)),
        top3_coverage=float(hit_rates.get(3, 0.0)),
        evidence_coverage=float(getattr(metrics, "citation_coverage", 0.0)),
        refusal_accuracy=float(getattr(metrics, "refusal_accuracy", 0.0)),
        latency_p95_ms=float(getattr(metrics, "latency_p95_ms", 0.0)),
        per_item=list(report.get("per_item", []) or getattr(metrics, "per_item", []) or []),
        raw_report=dict(report),
    )


def to_unified_rca(replay_result: dict[str, Any]) -> UnifiedReport:
    """Adapt an RCA replay result dict into UnifiedReport.

    - ``top1_coverage`` / ``top3_coverage`` map to Top-1 / Top-3 root-cause
      coverage.
    - ``evidence_coverage`` maps to RCA evidence coverage.
    - ``refusal_accuracy`` and ``latency_p95_ms`` are ``None`` for RCA.
    """
    return UnifiedReport(
        eval_type="rca",
        total=int(replay_result.get("total_cases", 0)),
        top1_coverage=float(replay_result.get("top1_root_cause_coverage", 0.0)),
        top3_coverage=float(replay_result.get("top3_root_cause_coverage", 0.0)),
        evidence_coverage=float(replay_result.get("evidence_coverage", 0.0)),
        refusal_accuracy=None,
        latency_p95_ms=None,
        per_item=list(replay_result.get("cases", []) or []),
        raw_report=dict(replay_result),
    )


__all__ = ["UnifiedReport", "to_unified_rag", "to_unified_rca"]
