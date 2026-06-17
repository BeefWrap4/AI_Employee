import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_employee.common_schemas.embedding import (
    EmbeddingProvider,
    EmbeddingUnavailableError,
    StubEmbeddingProvider,
)
from ai_employee.knowledge_api.app import create_app as create_api_app
from ai_employee.knowledge_api.store import SQLiteStore


class _FailingProvider(EmbeddingProvider):
    """始终抛 EmbeddingUnavailableError 的 query provider。"""
    name = "failing"
    dim = 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("qwen api returned 401", cause="4xx")


def _upload_and_publish(client: TestClient, *, title: str, content: str,
                         metadata: dict, acl_tags: list[str]) -> str:
    r = client.post(
        "/api/v1/documents",
        files={"file": (f"{title}.md", content.encode("utf-8"), "text/markdown")},
        data={
            "title": title,
            "metadata_json": json.dumps(metadata),
            "acl_tags_json": json.dumps(acl_tags),
            "version": "v1",
            "mime_type": "text/markdown",
        },
    )
    assert r.status_code == 202
    doc_id = r.json()["doc_id"]
    client.post(f"/api/v1/documents/{doc_id}/publish")
    return doc_id


def _build_app_with_query_provider(workspace: Path, provider: EmbeddingProvider) -> TestClient:
    store = SQLiteStore(
        db_path=str(workspace / "k.sqlite3"),
        data_dir=str(workspace),
    )
    store.init_schema()
    app = create_api_app(store=store)
    # 替换 query provider
    from ai_employee.knowledge_api.app import _current_retrieval  # 不存在
    # 通过 monkey-patching 替换 retrieval 的 query_provider
    # retrieve via app's retrieval service
    # 注入方式：替换 app state 中构建的 retrieval
    # 简化：直接 patch 整个 RetrievalService 实例
    import ai_employee.knowledge_api.app as app_mod
    from ai_employee.knowledge_api.retrieval import RetrievalService
    app_mod.retrieval.query_provider = provider
    return TestClient(app)


def test_chat_query_returns_503_when_query_provider_fails(knowledge_workspace: Path) -> None:
    raw = knowledge_workspace / "raw"
    raw.mkdir(exist_ok=True)
    # 绕过 upload（其自身会调 worker，而 worker 注入 query provider），直接 store 注入
    store = SQLiteStore(
        db_path=str(knowledge_workspace / "k.sqlite3"),
        data_dir=str(knowledge_workspace),
    )
    store.init_schema()
    doc_id = store.create_document("SOP", str(raw / "x.md"), "text/plain", {"network_type": "5g"}, ["wireless"], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": "RRC 建立失败先查告警", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")

    app = create_api_app(store=store)
    # 注入失败的 query provider
    app.state.retrieval.query_provider = _FailingProvider()

    client = TestClient(app)
    r = client.post(
        "/api/v1/chat/query",
        json={"session_id": "s1", "question": "RRC", "knowledge_scopes": ["wireless"], "stream": False},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "embedding_unavailable"
    # 响应含 trace_id
    assert "trace_id" in r.json()["detail"]


def test_chat_query_with_stub_provider_works_normally(knowledge_workspace: Path) -> None:
    raw = knowledge_workspace / "raw"
    raw.mkdir(exist_ok=True)
    store = SQLiteStore(
        db_path=str(knowledge_workspace / "k.sqlite3"),
        data_dir=str(knowledge_workspace),
    )
    store.init_schema()
    doc_id = store.create_document("SOP", str(raw / "x.md"), "text/plain", {"network_type": "5g"}, ["wireless"], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": "RRC 建立失败先查告警", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")

    app = create_api_app(store=store)
    # 默认 query provider 是 stub
    client = TestClient(app)
    r = client.post(
        "/api/v1/chat/query",
        json={"session_id": "s2", "question": "RRC 建立失败先查告警", "knowledge_scopes": ["wireless"], "stream": False},
    )
    assert r.status_code == 200


def test_chat_query_503_does_not_write_qa_log(knowledge_workspace: Path) -> None:
    raw = knowledge_workspace / "raw"
    raw.mkdir(exist_ok=True)
    store = SQLiteStore(
        db_path=str(knowledge_workspace / "k.sqlite3"),
        data_dir=str(knowledge_workspace),
    )
    store.init_schema()
    doc_id = store.create_document("SOP", str(raw / "x.md"), "text/plain", {"network_type": "5g"}, ["wireless"], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")

    app = create_api_app(store=store)
    app.state.retrieval.query_provider = _FailingProvider()

    client = TestClient(app)
    r = client.post(
        "/api/v1/chat/query",
        json={"session_id": "s3", "question": "x", "knowledge_scopes": ["wireless"], "stream": False},
    )
    assert r.status_code == 503

    # 验证 qa_log 没有这次请求的记录
    items, total = store.list_qa_logs(session_id="s3")
    assert total == 0
