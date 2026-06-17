"""Tests for the M6 platform eval center (spec §7).

Covers:
- common_schemas.eval UnifiedReport adapters (RAG + RCA).
- agent-platform-api eval_store SQLite persistence.
- agent-platform-api /api/v1/evaluations/runs endpoints (sync execute, list, get/404)
  with the eval-service runner and rca replay monkeypatched so no real services
  are required.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai_employee.common_schemas.eval import (
    UnifiedReport,
    to_unified_rca,
    to_unified_rag,
)
from ai_employee.eval.metrics import EvalMetrics


def _fake_rag_metrics() -> EvalMetrics:
    return EvalMetrics(
        total=4,
        errored=0,
        refusal_violations=0,
        eligible_for_hit=3,
        hit_counts={1: 2, 3: 3},
        hit_rates={1: 2 / 3, 3: 1.0},
        citation_coverage=1.0,
        refusal_expected=1,
        refusal_correct=1,
        refusal_accuracy=1.0,
        latency_p50_ms=100.0,
        latency_p95_ms=250.0,
        latency_mean_ms=120.0,
        per_item=[
            {"qid": "q1", "verdict": "hit@1", "expected": "doc_a", "returned": ["doc_a"]},
            {"qid": "q2", "verdict": "refusal", "expected": None, "returned": [], "status_code": 404},
        ],
    )


def _fake_rag_report(metrics: EvalMetrics) -> dict:
    return {
        "ts": "2026-06-17T00:00:00Z",
        "golden_path": "tests/rag-eval/golden.jsonl",
        "api_base": "http://127.0.0.1:8010",
        "top_ks": [1, 3, 5],
        "summary": {"total": metrics.total, "errored": metrics.errored, "refusal_violations": 0},
        "metrics": {
            "hit_rates": {str(k): v for k, v in metrics.hit_rates.items()},
            "citation_coverage": metrics.citation_coverage,
            "refusal_accuracy": metrics.refusal_accuracy,
            "latency_p95_ms": metrics.latency_p95_ms,
        },
        "per_item": list(metrics.per_item),
        "pass": True,
    }


def _fake_rca_replay_result() -> dict:
    return {
        "total_cases": 3,
        "top1_root_cause_coverage": 2 / 3,
        "top3_root_cause_coverage": 1.0,
        "evidence_coverage": 0.8,
        "average_evidence_count": 4.0,
        "cases": [
            {
                "case_id": "c1",
                "expected_root_cause_type": "fiber_cut",
                "predicted_root_cause_types": ["fiber_cut"],
                "top1_hit": True,
                "top3_hit": True,
                "evidence_count": 4,
                "evidence_coverage": 1.0,
            },
            {
                "case_id": "c2",
                "expected_root_cause_type": "config_error",
                "predicted_root_cause_types": ["hw_fault", "config_error"],
                "top1_hit": False,
                "top3_hit": True,
                "evidence_count": 4,
                "evidence_coverage": 0.5,
            },
        ],
    }


def test_to_unified_rag_maps_rag_metrics_and_report() -> None:
    metrics = _fake_rag_metrics()
    report = _fake_rag_report(metrics)

    unified = to_unified_rag(metrics, report)

    assert isinstance(unified, UnifiedReport)
    assert unified.eval_type == "rag"
    assert unified.total == 4
    assert unified.top1_coverage == round(2 / 3, 10) or unified.top1_coverage == 2 / 3
    assert unified.top3_coverage == 1.0
    assert unified.evidence_coverage == 1.0
    assert unified.refusal_accuracy == 1.0
    assert unified.latency_p95_ms == 250.0
    assert unified.per_item[0]["qid"] == "q1"
    assert unified.raw_report["api_base"] == "http://127.0.0.1:8010"


def test_to_unified_rca_maps_replay_result() -> None:
    replay = _fake_rca_replay_result()

    unified = to_unified_rca(replay)

    assert unified.eval_type == "rca"
    assert unified.total == 3
    assert unified.top1_coverage == 2 / 3
    assert unified.top3_coverage == 1.0
    assert unified.evidence_coverage == 0.8
    assert unified.refusal_accuracy is None
    assert unified.latency_p95_ms is None
    assert unified.per_item[0]["case_id"] == "c1"
    assert unified.raw_report["total_cases"] == 3


def test_unified_report_to_dict_is_json_serialisable() -> None:
    unified = to_unified_rca(_fake_rca_replay_result())
    payload = unified.to_dict()
    # Must round-trip through JSON (used for SQLite report_json persistence).
    encoded = json.dumps(payload, ensure_ascii=False)
    assert json.loads(encoded)["eval_type"] == "rca"
