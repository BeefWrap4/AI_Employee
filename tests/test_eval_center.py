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

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.eval_store import EvalStore
from ai_employee.common_schemas.eval import (
    UnifiedReport,
    to_unified_rag,
    to_unified_rca,
)
from ai_employee.eval.metrics import EvalMetrics, EvalResult
from fastapi.testclient import TestClient


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
            {
                "qid": "q2",
                "verdict": "refusal",
                "expected": None,
                "returned": [],
                "status_code": 404,
            },
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


# --------------------------------------------------------------------------- #
# eval_store SQLite persistence
# --------------------------------------------------------------------------- #


def test_eval_store_create_get_complete_round_trip(tmp_path) -> None:
    store = EvalStore(db_path=str(tmp_path / "eval.sqlite3"))

    eval_run_id = store.create_eval_run(
        eval_type="rag",
        template_id="knowledge_query",
        golden_path="tests/rag-eval/golden.jsonl",
    )

    assert eval_run_id == "eval_001"
    record = store.get_eval_run(eval_run_id)
    assert record is not None
    assert record["eval_type"] == "rag"
    assert record["status"] == "running"
    assert record["report_json"] is None
    assert record["trace_id"] == "trace_eval_001"
    assert record["completed_at"] is None

    unified = to_unified_rag(_fake_rag_metrics(), _fake_rag_report(_fake_rag_metrics()))
    store.complete_eval_run(
        eval_run_id,
        report=unified.to_dict(),
        summary={
            "total": unified.total,
            "top1_coverage": unified.top1_coverage,
            "top3_coverage": unified.top3_coverage,
            "evidence_coverage": unified.evidence_coverage,
        },
    )

    completed = store.get_eval_run(eval_run_id)
    assert completed["status"] == "completed"
    assert completed["report_json"]["eval_type"] == "rag"
    assert completed["summary"]["total"] == 4
    assert completed["completed_at"] is not None


def test_eval_store_list_filters_and_paginates(tmp_path) -> None:
    store = EvalStore(db_path=str(tmp_path / "eval.sqlite3"))
    store.create_eval_run(eval_type="rag", template_id="knowledge_query", golden_path="a")
    store.create_eval_run(eval_type="rca", template_id="rca", golden_path="b")
    store.create_eval_run(eval_type="rag", template_id="knowledge_query", golden_path="c")

    rag_rows, rag_total = store.list_eval_runs(eval_type="rag")
    assert rag_total == 2
    assert [r["eval_type"] for r in rag_rows] == ["rag", "rag"]

    rca_rows, rca_total = store.list_eval_runs(eval_type="rca")
    assert rca_total == 1
    assert rca_rows[0]["golden_path"] == "b"

    # status filter
    store.complete_eval_run(
        rag_rows[0]["eval_run_id"],
        report={"eval_type": "rag"},
        summary={"total": 0},
    )
    done_rows, done_total = store.list_eval_runs(status="completed")
    assert done_total == 1
    assert done_rows[0]["status"] == "completed"

    # pagination
    page1, _ = store.list_eval_runs(page=1, page_size=2)
    page2, _ = store.list_eval_runs(page=2, page_size=2)
    assert len(page1) == 2
    assert len(page2) == 1
    assert page2[0]["eval_run_id"] == "eval_003"


def test_eval_store_get_missing_returns_none(tmp_path) -> None:
    store = EvalStore(db_path=str(tmp_path / "eval.sqlite3"))
    assert store.get_eval_run("eval_999") is None


# --------------------------------------------------------------------------- #
# /api/v1/evaluations/runs endpoints (sync execute, list, get/404)
# --------------------------------------------------------------------------- #


@pytest.fixture
def eval_client(tmp_path, monkeypatch):
    """Isolated platform app whose eval_runs SQLite lives under tmp_path."""
    monkeypatch.setenv("RCA_DATA_DIR", str(tmp_path))
    return TestClient(create_app())


def _fake_rag_results() -> list[EvalResult]:
    return [
        EvalResult(
            qid="q1",
            question="How to triage RRC failures?",
            expected_doc_id="doc_a",
            expect_refusal=False,
            status_code=200,
            returned_doc_ids=["doc_a"],
            answer="Triage per runbook.",
            latency_ms=100,
        ),
        EvalResult(
            qid="q2",
            question="something out of scope",
            expected_doc_id=None,
            expect_refusal=True,
            status_code=404,
            returned_doc_ids=[],
            answer="",
            latency_ms=50,
        ),
    ]


def test_post_rag_eval_run_executes_and_persists_unified_report(eval_client, monkeypatch) -> None:
    monkeypatch.setattr("ai_employee.eval.runner.run", lambda **kwargs: _fake_rag_results())

    response = eval_client.post(
        "/api/v1/evaluations/runs",
        json={
            "eval_type": "rag",
            "template_id": "knowledge_query",
            "golden_path": "tests/rag-eval/golden.jsonl",
            "api_base": "http://127.0.0.1:8010",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["eval_run_id"].startswith("eval_")
    assert body["eval_type"] == "rag"
    assert body["status"] == "completed"
    assert body["completed_at"] is not None
    report = body["report"]
    assert report["eval_type"] == "rag"
    assert report["total"] == 2
    assert report["top1_coverage"] == 1.0
    assert report["top3_coverage"] == 1.0
    assert report["evidence_coverage"] == 1.0
    assert report["refusal_accuracy"] == 1.0
    assert report["latency_p95_ms"] is not None
    assert len(report["per_item"]) == 2
    assert body["summary"]["total"] == 2
    assert body["summary"]["top1_coverage"] == 1.0

    # Persisted: GET single returns the same report.
    fetched = eval_client.get(f"/api/v1/evaluations/runs/{body['eval_run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["report"]["total"] == 2
    assert fetched.json()["trace_id"].startswith("trace_eval_")


def test_post_rca_eval_run_executes_and_persists_unified_report(eval_client, monkeypatch) -> None:
    monkeypatch.setattr(
        "ai_employee.rca_agent.replay.run_replay_file",
        lambda path: _fake_rca_replay_result(),
    )

    response = eval_client.post(
        "/api/v1/evaluations/runs",
        json={
            "eval_type": "rca",
            "template_id": "rca",
            "golden_path": "tests/rca-replay/cases.jsonl",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["eval_type"] == "rca"
    assert body["status"] == "completed"
    report = body["report"]
    assert report["eval_type"] == "rca"
    assert report["total"] == 3
    assert report["top1_coverage"] == 2 / 3
    assert report["top3_coverage"] == 1.0
    assert report["evidence_coverage"] == 0.8
    assert report["refusal_accuracy"] is None
    assert report["latency_p95_ms"] is None
    assert report["per_item"][0]["case_id"] == "c1"
    assert body["summary"]["evidence_coverage"] == 0.8


def test_post_rag_eval_without_api_base_returns_400(eval_client, monkeypatch) -> None:
    response = eval_client.post(
        "/api/v1/evaluations/runs",
        json={
            "eval_type": "rag",
            "template_id": "knowledge_query",
            "golden_path": "tests/rag-eval/golden.jsonl",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "eval_invalid_request"
    # The failed run is persisted with status=failed for auditability.
    listed = eval_client.get("/api/v1/evaluations/runs?status=failed")
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["error"].startswith("api_base is required")


def test_list_eval_runs_filters_by_eval_type(eval_client, monkeypatch) -> None:
    monkeypatch.setattr("ai_employee.eval.runner.run", lambda **kwargs: _fake_rag_results())
    monkeypatch.setattr(
        "ai_employee.rca_agent.replay.run_replay_file",
        lambda path: _fake_rca_replay_result(),
    )

    eval_client.post(
        "/api/v1/evaluations/runs",
        json={
            "eval_type": "rag",
            "template_id": "knowledge_query",
            "golden_path": "a",
            "api_base": "http://127.0.0.1:8010",
        },
    )
    eval_client.post(
        "/api/v1/evaluations/runs",
        json={"eval_type": "rca", "template_id": "rca", "golden_path": "b"},
    )

    rag_list = eval_client.get("/api/v1/evaluations/runs?eval_type=rag")
    assert rag_list.status_code == 200
    rag_body = rag_list.json()
    assert rag_body["total"] == 1
    assert rag_body["items"][0]["eval_type"] == "rag"
    assert rag_body["items"][0]["status"] == "completed"
    # List items carry the summary but not the full report payload.
    assert "report" not in rag_body["items"][0]
    assert rag_body["items"][0]["summary"]["total"] == 2

    rca_list = eval_client.get("/api/v1/evaluations/runs?eval_type=rca")
    assert rca_list.json()["total"] == 1
    assert rca_list.json()["items"][0]["eval_type"] == "rca"

    all_list = eval_client.get("/api/v1/evaluations/runs")
    assert all_list.json()["total"] == 2


def test_get_eval_run_missing_returns_404(eval_client) -> None:
    response = eval_client.get("/api/v1/evaluations/runs/eval_999")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "eval_run_not_found"
    assert response.json()["detail"]["eval_run_id"] == "eval_999"


def test_list_eval_runs_paginates(eval_client, monkeypatch) -> None:
    monkeypatch.setattr(
        "ai_employee.rca_agent.replay.run_replay_file",
        lambda path: _fake_rca_replay_result(),
    )
    for _ in range(3):
        eval_client.post(
            "/api/v1/evaluations/runs",
            json={"eval_type": "rca", "template_id": "rca", "golden_path": "g"},
        )

    page1 = eval_client.get("/api/v1/evaluations/runs?page=1&page_size=2")
    page2 = eval_client.get("/api/v1/evaluations/runs?page=2&page_size=2")
    assert page1.json()["total"] == 3
    assert len(page1.json()["items"]) == 2
    assert len(page2.json()["items"]) == 1
    assert page2.json()["items"][0]["eval_run_id"] == "eval_003"
