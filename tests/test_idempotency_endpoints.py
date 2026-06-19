"""Idempotency-Key endpoint integration tests (R23-2).

A retried POST carrying the same ``Idempotency-Key`` must not
re-execute the side effect; the second response carries the original
result (same run_id / eval_run_id) and a 201 (success) status.  A
request without the header behaves as before.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.common_schemas.idempotency import InMemoryIdempotencyStore
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# POST /api/v1/agent-runs
# --------------------------------------------------------------------------- #


def _knowledge_qa_payload() -> dict:
    return {
        "template_id": "knowledge_qa",
        "requested_by": "noc_user",
        "input": {
            "question": "How should RRC setup failures be triaged?",
            "knowledge_scopes": ["wireless"],
        },
    }


def test_agent_run_without_key_creates_two_distinct_runs() -> None:
    """No Idempotency-Key → each POST is a fresh run."""
    client = TestClient(create_app())
    r1 = client.post("/api/v1/agent-runs", json=_knowledge_qa_payload())
    r2 = client.post("/api/v1/agent-runs", json=_knowledge_qa_payload())
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_agent_run_same_key_replays_cached_result() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(create_app(idempotency_store=store))
    headers = {"Idempotency-Key": "abc-123"}
    r1 = client.post("/api/v1/agent-runs", json=_knowledge_qa_payload(), headers=headers)
    r2 = client.post("/api/v1/agent-runs", json=_knowledge_qa_payload(), headers=headers)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["run_id"] == r2.json()["run_id"]
    assert r1.json()["trace_id"] == r2.json()["trace_id"]
    # The store recorded exactly one completed key.
    rec = store.get_or_begin("abc-123")
    assert rec.status == "success"


def test_agent_run_different_keys_create_distinct_runs() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(create_app(idempotency_store=store))
    r1 = client.post(
        "/api/v1/agent-runs",
        json=_knowledge_qa_payload(),
        headers={"Idempotency-Key": "k1"},
    )
    r2 = client.post(
        "/api/v1/agent-runs",
        json=_knowledge_qa_payload(),
        headers={"Idempotency-Key": "k2"},
    )
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_agent_run_empty_idempotency_key_ignored() -> None:
    """An empty header is treated as absent (no caching)."""
    store = InMemoryIdempotencyStore()
    client = TestClient(create_app(idempotency_store=store))
    r1 = client.post(
        "/api/v1/agent-runs", json=_knowledge_qa_payload(), headers={"Idempotency-Key": ""}
    )
    r2 = client.post(
        "/api/v1/agent-runs", json=_knowledge_qa_payload(), headers={"Idempotency-Key": ""}
    )
    assert r1.json()["run_id"] != r2.json()["run_id"]


# --------------------------------------------------------------------------- #
# POST /api/v1/evaluations/runs
# --------------------------------------------------------------------------- #


def _rag_eval_payload(golden_path: str = "tests/rag-eval/golden.jsonl") -> dict:
    return {
        "eval_type": "rag",
        "template_id": "knowledge_query",
        "golden_path": golden_path,
        "api_base": "http://127.0.0.1:8010",
    }


def _patch_rag_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch the eval runner so no live HTTP / golden file is needed."""
    from ai_employee.eval.metrics import EvalResult

    def _fake_results(**_kwargs):
        return [
            EvalResult(
                qid="q1",
                question="q",
                expected_doc_id="doc_a",
                expect_refusal=False,
                status_code=200,
                returned_doc_ids=["doc_a"],
                answer="a",
                latency_ms=10,
            ),
        ]

    monkeypatch.setattr("ai_employee.eval.runner.run", lambda **kw: _fake_results())


def test_eval_run_without_key_creates_two_distinct_runs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCA_DATA_DIR", str(tmp_path))
    _patch_rag_runner(monkeypatch)
    client = TestClient(create_app())
    r1 = client.post("/api/v1/evaluations/runs", json=_rag_eval_payload())
    r2 = client.post("/api/v1/evaluations/runs", json=_rag_eval_payload())
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["eval_run_id"] != r2.json()["eval_run_id"]


def test_eval_run_same_key_replays_cached_result(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCA_DATA_DIR", str(tmp_path))
    _patch_rag_runner(monkeypatch)
    store = InMemoryIdempotencyStore()
    client = TestClient(create_app(idempotency_store=store))
    headers = {"Idempotency-Key": "eval-1"}
    r1 = client.post("/api/v1/evaluations/runs", json=_rag_eval_payload(), headers=headers)
    r2 = client.post("/api/v1/evaluations/runs", json=_rag_eval_payload(), headers=headers)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["eval_run_id"] == r2.json()["eval_run_id"]


# --------------------------------------------------------------------------- #
# POST /api/v1/documents (knowledge-api)
# --------------------------------------------------------------------------- #


def _setup_knowledge_workspace(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Mirror the knowledge_workspace fixture without importing tests.conftest."""
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "knowledge.sqlite3"
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KNOWLEDGE_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("KNOWLEDGE_API_INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("INGESTION_WORKER_URL", "http://in-process")
    monkeypatch.delenv("OBJECT_STORE_URL", raising=False)
    monkeypatch.setenv("OBJECT_STORE_LOCAL_ROOT", str(tmp_path / "objects"))
    return data_dir


def _in_process_worker():
    """Build an in-process worker client (avoids importing tests.conftest)."""
    from ai_employee.common_schemas.knowledge import ParseResponse
    from ai_employee.ingestion_worker.app import create_app as create_worker_app
    from ai_employee.knowledge_api.worker_client import (
        WorkerClient,
        WorkerDispatchResult,
    )

    class InProcessWorker(WorkerClient):
        def __init__(self) -> None:
            self._client = TestClient(create_worker_app())
            self._reachable = True

        def health(self) -> bool:
            return self._reachable

        def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
            resp = self._client.post(
                "/internal/parse",
                json={
                    "doc_id": doc_id,
                    "file_path": file_path,
                    "mime_type": mime_type,
                    "metadata": metadata,
                },
            )
            if resp.status_code == 200:
                return WorkerDispatchResult(
                    dispatched=True,
                    dispatch_status="accepted",
                    response=ParseResponse(**resp.json()),
                )
            return WorkerDispatchResult(
                dispatched=False,
                dispatch_status="worker_error",
                error=f"worker returned {resp.status_code}",
            )

    return InProcessWorker()


def test_document_upload_same_key_and_content_replays(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same Idempotency-Key + same content -> replay the cached doc_id."""
    from ai_employee.knowledge_api.app import create_app as create_knowledge_app

    _setup_knowledge_workspace(tmp_path, monkeypatch)
    store = InMemoryIdempotencyStore()
    client = TestClient(
        create_knowledge_app(worker_client=_in_process_worker(), idempotency_store=store)
    )
    headers = {
        "Idempotency-Key": "doc-1",
        "X-Internal-Token": "test-token",
    }
    body = b"# title\nhello world"
    files = {"file": ("doc.md", body, "text/markdown")}
    data = {"title": "doc", "metadata_json": "{}", "acl_tags_json": "[]", "version": "v1"}
    r1 = client.post("/api/v1/documents", files=files, data=data, headers=headers)
    r2 = client.post("/api/v1/documents", files=files, data=data, headers=headers)
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert r1.json()["doc_id"] == r2.json()["doc_id"]


def test_document_upload_same_key_different_content_re_executes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same key but different content -> content-hash mismatch -> new doc."""
    from ai_employee.knowledge_api.app import create_app as create_knowledge_app

    _setup_knowledge_workspace(tmp_path, monkeypatch)
    store = InMemoryIdempotencyStore()
    client = TestClient(
        create_knowledge_app(worker_client=_in_process_worker(), idempotency_store=store)
    )
    headers = {
        "Idempotency-Key": "doc-2",
        "X-Internal-Token": "test-token",
    }
    data = {"title": "doc", "metadata_json": "{}", "acl_tags_json": "[]", "version": "v1"}
    r1 = client.post(
        "/api/v1/documents",
        files={"file": ("doc.md", b"# title\ncontent A", "text/markdown")},
        data=data,
        headers=headers,
    )
    r2 = client.post(
        "/api/v1/documents",
        files={"file": ("doc.md", b"# title\ncontent B", "text/markdown")},
        data=data,
        headers=headers,
    )
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert r1.json()["doc_id"] != r2.json()["doc_id"]
