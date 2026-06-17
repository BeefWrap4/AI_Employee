from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_employee.common_schemas.knowledge import ParseResponse, ParsedChunk
from ai_employee.knowledge_api.app import create_app as create_api_app
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult


class FailingWorkerClient(WorkerClient):
    """模拟 worker 处理失败（mime_unsupported 等 worker_error 路径）。"""

    def __init__(self) -> None:
        self._reachable = True
        self._calls = 0

    def health(self) -> bool:
        return self._reachable

    def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
        self._calls += 1
        if self._calls == 1:
            # 首次：worker 返回 worker_error（如 mime_unsupported）
            return WorkerDispatchResult(
                dispatched=False,
                dispatch_status="worker_error",
                error="mime_unsupported: application/pdf",
            )
        # 第二次（reparse）：成功
        chunk = ParsedChunk(
            chunk_id=f"chunk_{doc_id}_001",
            chunk_no=1,
            content="重试后解析成功的内容。",
            section_path="root",
        )
        return WorkerDispatchResult(
            dispatched=True,
            dispatch_status="accepted",
            response=ParseResponse(
                doc_id=doc_id,
                chunks=[chunk],
                embeddings=[[0.0] * 8],
                embedding_model="stub",
            ),
        )


def _make_client(api_factory) -> tuple[TestClient, FailingWorkerClient]:
    wc = FailingWorkerClient()
    return api_factory(worker_client=wc), wc


def _upload(client: TestClient, title: str, content: bytes, mime_type: str):
    return client.post(
        "/api/v1/documents",
        files={"file": (f"{title}.bin", content, mime_type)},
        data={
            "title": title,
            "metadata_json": "{}",
            "acl_tags_json": "[]",
            "version": "v1",
            "mime_type": mime_type,
        },
    )


def test_parse_failure_then_reparse_recovers(api_factory) -> None:
    client, _ = _make_client(api_factory)

    # 用受支持的 mime 但 worker 模拟失败（worker_error 路径）
    created = _upload(client, "SOP", b"fake content", "text/markdown")
    assert created.status_code == 202
    body = created.json()
    doc_id = body["doc_id"]
    assert body["parse_status"] == "parse_failed"
    assert body["worker_dispatch"] == "worker_error"

    # reparse 前必须处于 parse_failed
    fetched = client.get(f"/api/v1/documents/{doc_id}")
    assert fetched.json()["parse_status"] == "parse_failed"
    assert "mime_unsupported" in (fetched.json()["parse_error"] or "")

    # reparse 恢复
    reparsed = client.post(f"/api/v1/documents/{doc_id}/reparse")
    assert reparsed.status_code == 200
    assert reparsed.json()["parse_status"] == "ready"
    assert reparsed.json()["worker_dispatch"] == "accepted"


def test_reparse_requires_parse_failed(api_factory) -> None:
    client, _ = _make_client(api_factory)
    created = _upload(client, "SOP", b"fake", "text/markdown")
    doc_id = created.json()["doc_id"]
    # 此时已 parse_failed；先 reparse 成功到 ready
    client.post(f"/api/v1/documents/{doc_id}/reparse")
    # 再 reparse：现在是 ready，应 409
    resp = client.post(f"/api/v1/documents/{doc_id}/reparse")
    assert resp.status_code == 409
