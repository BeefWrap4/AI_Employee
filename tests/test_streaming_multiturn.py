"""Streaming + multi-turn + new metrics tests (spec §5.5 + §5.6)."""
from __future__ import annotations

import json

from ai_employee.eval.metrics import EvalResult, compute
from fastapi.testclient import TestClient


def _upload_and_publish(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/documents",
        data={
            "title": "RRC 排障 SOP",
            "metadata_json": json.dumps({"network_type": "5g"}),
            "acl_tags_json": json.dumps(["wireless"]),
            "version": "v1",
            "mime_type": "text/markdown",
        },
        files={"file": ("sop.md", "# RRC\n\nRRC 建立失败先查告警 KPI。".encode(), "text/markdown")},
    )
    doc_id = resp.json()["doc_id"]
    client.post(f"/api/v1/documents/{doc_id}/publish")
    return doc_id


def test_query_session_endpoint_returns_history(api_factory) -> None:
    client = api_factory()
    _upload_and_publish(client)
    r1 = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s1",
            "question": "RRC 建立失败先查告警 KPI",
            "knowledge_scopes": ["wireless"],
        },
    )
    assert r1.status_code == 200
    history = client.get("/api/v1/chat/sessions/s1/history").json()
    assert history["total"] >= 1
    assert any(t["question"] == "RRC 建立失败先查告警 KPI" for t in history["turns"])


def test_streaming_endpoint_emits_sse_events(api_factory) -> None:
    client = api_factory()
    _upload_and_publish(client)
    r = client.post(
        "/api/v1/chat/query/stream",
        json={
            "session_id": "s-stream",
            "question": "RRC 建立失败先查告警 KPI",
            "knowledge_scopes": ["wireless"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: meta" in body
    assert "event: token" in body
    assert "event: citations" in body
    assert "event: done" in body


def test_compute_metrics_includes_faithfulness_and_relevance() -> None:
    results = [
        EvalResult(
            qid="q1",
            question="RRC 失败排查?",
            expected_doc_id="d1",
            expect_refusal=False,
            status_code=200,
            returned_doc_ids=["d1"],
            answer="RRC 建立失败时检查告警 KPI 和传输链路。",
            latency_ms=100,
            expected_answer_keywords=["RRC", "KPI", "传输"],
            expected_answer_text="RRC 建立失败时应检查告警 KPI。",
        ),
        EvalResult(
            qid="q2",
            question="另一个问题",
            expected_doc_id="d2",
            expect_refusal=False,
            status_code=200,
            returned_doc_ids=["d2"],
            answer="答案。",
            latency_ms=200,
        ),
    ]
    metrics = compute(results, top_ks=[1, 3])
    # q1 has 3/3 keyword overlap → 1.0
    assert metrics.faithfulness == 1.0
    # q1 has overlapping tokens (rrc, 建立失败, 检查, 告警, kpi, 应)
    assert metrics.answer_relevance > 0
    # q2 contributes no faithfulness / relevance (denominator is 0)
    assert metrics.faithfulness_eligible == 1
    assert metrics.answer_relevance_eligible == 1


def test_compute_metrics_without_ground_truth_keeps_zero() -> None:
    """When no expected_answer_* is supplied, faithfulness/relevance are 0
    but no crash."""
    results = [
        EvalResult(
            qid="q1",
            question="x",
            expected_doc_id="d1",
            expect_refusal=False,
            status_code=200,
            returned_doc_ids=["d1"],
            answer="hello world",
            latency_ms=50,
        )
    ]
    metrics = compute(results, top_ks=[1])
    assert metrics.faithfulness == 0.0
    assert metrics.answer_relevance == 0.0
