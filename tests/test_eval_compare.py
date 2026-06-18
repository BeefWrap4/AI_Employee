"""Eval Center version comparison unit + API tests."""

from __future__ import annotations

from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.eval_compare import (
    compare_reports,
    load_unified_report,
)
from ai_employee.agent_platform_api.eval_store import EvalStore
from ai_employee.common_schemas.eval import UnifiedReport
from fastapi.testclient import TestClient


def _record(report: UnifiedReport, summary: dict) -> dict:
    return {
        "eval_run_id": "eval_x",
        "eval_type": report.eval_type,
        "template_id": "tpl",
        "golden_path": "x.jsonl",
        "status": "completed",
        "trace_id": "trace_x",
        "created_at": "2026-06-01T00:00:00Z",
        "completed_at": "2026-06-01T00:01:00Z",
        "report_json": report.to_dict(),
        "summary": summary,
    }


def test_compare_rag_reports_computes_deltas() -> None:
    a = UnifiedReport(
        eval_type="rag",
        total=10,
        top1_coverage=0.6,
        top3_coverage=0.8,
        evidence_coverage=0.7,
        refusal_accuracy=0.9,
        latency_p95_ms=120.0,
    )
    b = UnifiedReport(
        eval_type="rag",
        total=10,
        top1_coverage=0.7,
        top3_coverage=0.85,
        evidence_coverage=0.65,
        refusal_accuracy=0.85,
        latency_p95_ms=150.0,
    )
    result = compare_reports(_record(a, {}), _record(b, {}))
    by_metric = {m["metric"]: m for m in result["metrics"]}
    assert by_metric["top1_coverage"]["delta"] == 0.1
    assert by_metric["top1_coverage"]["direction"] == "up"
    assert by_metric["evidence_coverage"]["delta"] == -0.05
    assert by_metric["evidence_coverage"]["direction"] == "down"
    assert by_metric["latency_p95_ms"]["delta"] == 30.0
    assert by_metric["latency_p95_ms"]["direction"] == "up"


def test_compare_rca_reports_omits_rag_only_metrics() -> None:
    a = UnifiedReport(
        eval_type="rca",
        total=5,
        top1_coverage=0.4,
        top3_coverage=0.8,
        evidence_coverage=0.5,
        refusal_accuracy=None,
        latency_p95_ms=None,
    )
    b = UnifiedReport(
        eval_type="rca",
        total=5,
        top1_coverage=0.6,
        top3_coverage=0.9,
        evidence_coverage=0.7,
        refusal_accuracy=None,
        latency_p95_ms=None,
    )
    result = compare_reports(_record(a, {}), _record(b, {}))
    metric_names = {m["metric"] for m in result["metrics"]}
    assert "refusal_accuracy" not in metric_names
    assert "latency_p95_ms" not in metric_names
    assert metric_names == {"top1_coverage", "top3_coverage", "evidence_coverage"}


def test_compare_handles_null_metric_values() -> None:
    a = UnifiedReport(
        eval_type="rag",
        total=0,
        top1_coverage=0.0,
        top3_coverage=0.0,
        evidence_coverage=0.0,
        refusal_accuracy=None,
        latency_p95_ms=None,
    )
    b = UnifiedReport(
        eval_type="rag",
        total=0,
        top1_coverage=0.0,
        top3_coverage=0.0,
        evidence_coverage=0.0,
        refusal_accuracy=None,
        latency_p95_ms=None,
    )
    result = compare_reports(_record(a, {}), _record(b, {}))
    by_metric = {m["metric"]: m for m in result["metrics"]}
    for name in ("refusal_accuracy", "latency_p95_ms"):
        assert by_metric[name]["a"] is None
        assert by_metric[name]["b"] is None
        assert by_metric[name]["delta"] is None
        assert by_metric[name]["direction"] == "unknown"


def test_compare_cross_type_only_uses_common_metrics() -> None:
    a = UnifiedReport(
        eval_type="rag",
        total=10,
        top1_coverage=0.5,
        top3_coverage=0.7,
        evidence_coverage=0.6,
        refusal_accuracy=0.8,
        latency_p95_ms=100.0,
    )
    b = UnifiedReport(
        eval_type="rca",
        total=5,
        top1_coverage=0.4,
        top3_coverage=0.8,
        evidence_coverage=0.5,
        refusal_accuracy=None,
        latency_p95_ms=None,
    )
    result = compare_reports(_record(a, {}), _record(b, {}))
    metric_names = {m["metric"] for m in result["metrics"]}
    assert metric_names == {"top1_coverage", "top3_coverage", "evidence_coverage"}


def test_load_unified_report_falls_back_to_minimal_when_missing() -> None:
    record = {"eval_type": "rag", "report_json": {}}
    report = load_unified_report(record)
    assert report.eval_type == "rag"
    assert report.total == 0


def test_compare_endpoint_returns_deltas(tmp_path) -> None:
    eval_store = EvalStore(db_path=str(tmp_path / "eval.sqlite3"))
    a_report = UnifiedReport(
        eval_type="rag",
        total=10,
        top1_coverage=0.5,
        top3_coverage=0.7,
        evidence_coverage=0.6,
        refusal_accuracy=0.8,
        latency_p95_ms=100.0,
    )
    b_report = UnifiedReport(
        eval_type="rag",
        total=10,
        top1_coverage=0.55,
        top3_coverage=0.75,
        evidence_coverage=0.62,
        refusal_accuracy=0.82,
        latency_p95_ms=95.0,
    )
    aid = eval_store.create_eval_run(
        eval_type="rag", template_id="knowledge_qa", golden_path="x.jsonl"
    )
    eval_store.complete_eval_run(aid, report=a_report.to_dict(), summary={"total": 10})
    bid = eval_store.create_eval_run(
        eval_type="rag", template_id="knowledge_qa", golden_path="x.jsonl"
    )
    eval_store.complete_eval_run(bid, report=b_report.to_dict(), summary={"total": 10})

    client = TestClient(create_app(eval_store=eval_store))
    resp = client.get(f"/api/v1/evaluations/compare?run_a={aid}&run_b={bid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["a"]["eval_run_id"] == aid
    assert body["b"]["eval_run_id"] == bid
    by_metric = {m["metric"]: m for m in body["metrics"]}
    assert by_metric["top1_coverage"]["delta"] == 0.05
    assert by_metric["latency_p95_ms"]["delta"] == -5.0


def test_compare_endpoint_returns_404_for_missing_runs(tmp_path) -> None:
    eval_store = EvalStore(db_path=str(tmp_path / "eval.sqlite3"))
    client = TestClient(create_app(eval_store=eval_store))
    resp = client.get("/api/v1/evaluations/compare?run_a=missing_a&run_b=missing_b")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "eval_run_not_found"
