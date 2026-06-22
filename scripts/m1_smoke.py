from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

# ruff: noqa: E402 — the warning filter must be installed before importing
# starlette.testclient, and the testclient-import itself comes last so the
# deprecation warning from httpx is suppressed.
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

from ai_employee.common_schemas.knowledge import ParseResponse
from ai_employee.ingestion_worker.app import create_app as create_worker_app
from ai_employee.knowledge_api.app import create_app as create_api_app
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult
from fastapi.testclient import TestClient


class InProcessWorkerClient(WorkerClient):
    def __init__(self) -> None:
        self._client = TestClient(create_worker_app())

    def health(self) -> bool:
        return True

    def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
        response = self._client.post(
            "/internal/parse",
            json={
                "doc_id": doc_id,
                "file_path": file_path,
                "mime_type": mime_type,
                "metadata": metadata,
            },
        )
        if response.status_code == 200:
            return WorkerDispatchResult(
                dispatched=True,
                dispatch_status="accepted",
                response=ParseResponse(**response.json()),
            )
        return WorkerDispatchResult(
            dispatched=False,
            dispatch_status="worker_error",
            error=f"worker returned {response.status_code}: {response.text}",
        )


def _client(data_dir: Path) -> TestClient:
    os.environ["KNOWLEDGE_DATA_DIR"] = str(data_dir)
    os.environ["KNOWLEDGE_SQLITE_PATH"] = str(data_dir / "knowledge.sqlite3")
    os.environ["KNOWLEDGE_API_INTERNAL_TOKEN"] = "local-smoke-token"
    os.environ["INGESTION_WORKER_URL"] = "http://in-process"
    os.environ["EMBEDDING_PROVIDER"] = "stub"
    os.environ["EMBEDDING_DIM"] = "8"
    data_dir.mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(
        db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
        data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
    )
    store.init_schema()
    return TestClient(create_api_app(store=store, worker_client=InProcessWorkerClient()),
                      headers={"X-Internal-Token": os.environ["KNOWLEDGE_API_INTERNAL_TOKEN"]})


def run_smoke(data_dir: Path) -> dict[str, Any]:
    client = _client(data_dir)
    # R24-A.4: the production write endpoints require authentication.
    # Local smoke scripts use the service-specific X-Internal-Token
    # fallback set on the TestClient above; production callers should
    # use OIDC or a JWT.
    content = (
        "# 5G RRC 建立失败处理 SOP\n\n"
        "RRC 建立失败时先检查无线侧告警和接入 KPI。\n\n"
        "如果伴随传输链路误码，继续核查端口误码、光功率和链路抖动。"
    )

    created = client.post(
        "/api/v1/documents",
        files={"file": ("rrc-sop.md", content.encode("utf-8"), "text/markdown")},
        data={
            "title": "5G RRC 建立失败处理 SOP",
            "metadata_json": json.dumps({"network_type": "5g", "domain": "wireless"}),
            "acl_tags_json": json.dumps(["wireless", "noc", "transport"]),
            "version": "v1",
            "mime_type": "text/markdown",
        },
    )
    created.raise_for_status()
    doc_id = created.json()["doc_id"]

    published = client.post(f"/api/v1/documents/{doc_id}/publish")
    published.raise_for_status()

    query = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "smoke",
            "question": "RRC 建立失败并伴随链路误码时先查什么？",
            "knowledge_scopes": ["wireless", "transport", "noc"],
            "stream": False,
        },
    )
    query.raise_for_status()
    query_body = query.json()

    feedback = client.post(
        "/api/v1/feedback",
        json={
            "trace_id": query_body["trace_id"],
            "feedback_type": "useful",
            "comment": "local smoke passed",
        },
    )
    feedback.raise_for_status()

    qa_logs = client.get("/api/v1/qa-logs")
    qa_logs.raise_for_status()
    feedbacks = client.get("/api/v1/feedbacks")
    feedbacks.raise_for_status()

    return {
        "document": {
            "doc_id": doc_id,
            "parse_status": published.json()["parse_status"],
            "chunk_count": published.json()["chunk_count"],
        },
        "query": {
            "trace_id": query_body["trace_id"],
            "confidence": query_body["confidence"],
            "citation_count": len(query_body["citations"]),
            "first_citation": query_body["citations"][0],
        },
        "feedback": feedback.json(),
        "audit": {
            "qa_log_total": qa_logs.json()["total"],
            "feedback_total": feedbacks.json()["total"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local M1 knowledge smoke flow.")
    parser.add_argument("--data-dir", default=None, help="data dir for SQLite and files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="ai-employee-smoke-"))
    summary = run_smoke(data_dir)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("M1 smoke flow passed")
        print(f"  document: {summary['document']['doc_id']} ({summary['document']['chunk_count']} chunks)")
        print(f"  query trace: {summary['query']['trace_id']}")
        print(f"  citations: {summary['query']['citation_count']}")
        print(f"  qa logs: {summary['audit']['qa_log_total']}")
        print(f"  feedbacks: {summary['audit']['feedback_total']}")
        print(f"  data dir: {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
