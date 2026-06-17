from pathlib import Path

import pytest

from ai_employee.ingestion_worker.embedding import StubEmbeddingProvider
from ai_employee.knowledge_api.retrieval import RetrievalService
from ai_employee.knowledge_api.store import SQLiteStore

_STUB = StubEmbeddingProvider(dim=8)


def _vec(text: str) -> list[float]:
    # 与 worker 的 StubEmbeddingProvider + retrieval 的 _embed_question 保持一致
    return _STUB.embed([text])[0]


@pytest.fixture
def service(tmp_path: Path) -> RetrievalService:
    store = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    store.init_schema()
    return RetrievalService(store)


def _publish(store: SQLiteStore, title: str, content: str, metadata: dict, acl_tags: list[str]) -> str:
    doc_id = store.create_document(title, "/tmp/x", "text/plain", metadata, acl_tags, "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"chunk_{doc_id}_001", "chunk_no": 1, "content": content, "section_path": "root"}],
        [_vec(content)],
        "stub",
    )
    store.transition_status(doc_id, "published")
    return doc_id


def test_vector_recall_returns_matching_chunk(service: RetrievalService) -> None:
    _publish(service.store, "RRC SOP", "RRC 建立失败时先检查告警和接入 KPI", {"network_type": "5g"}, ["wireless"])
    hits = service.search("RRC 建立失败时先检查告警和接入 KPI", ["wireless"], top_k=3)
    assert len(hits) == 1
    assert "告警" in hits[0].content
    assert hits[0].doc_title == "RRC SOP"


def test_search_filters_out_of_scope(service: RetrievalService) -> None:
    # 查询传输内容，scope 限定 wireless：transport 文档不进入候选，
    # 且 wireless 文档与查询相似度低 → 拒答 404（不越权返回 transport）。
    from fastapi import HTTPException

    _publish(service.store, "无线", "RRC 建立失败检查告警", {"network_type": "5g"}, ["wireless"])
    _publish(service.store, "传输", "光功率核查传输误码", {"network_type": "transport"}, ["transport"])
    with pytest.raises(HTTPException) as exc:
        service.search("光功率核查传输误码", ["wireless"], top_k=3)
    assert exc.value.status_code == 404


def test_no_results_raises_404(service: RetrievalService) -> None:
    from fastapi import HTTPException

    _publish(service.store, "无线", "RRC 建立失败检查告警", {"network_type": "5g"}, ["wireless"])
    # 完全不相关且 FTS 无命中、向量相似度低于阈值
    with pytest.raises(HTTPException) as exc:
        service.search("zzzqqqxxx", ["wireless"], top_k=3)
    assert exc.value.status_code == 404


def test_no_published_in_scope_raises_404(service: RetrievalService) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        service.search("anything", ["wireless"], top_k=3)
    assert exc.value.status_code == 404


def test_hit_has_positive_confidence(service: RetrievalService) -> None:
    _publish(service.store, "A", "RRC 建立失败处理", {"network_type": "5g"}, ["wireless"])
    hits = service.search("RRC 建立失败处理", ["wireless"], top_k=3)
    assert hits[0].confidence >= 0
