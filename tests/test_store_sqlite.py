from pathlib import Path

import pytest

from ai_employee.knowledge_api.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    s.init_schema()
    return s


def test_init_schema_creates_tables(store: SQLiteStore) -> None:
    tables = store.list_tables()
    for expected in ("documents", "chunks", "chunks_fts", "qa_logs", "feedbacks"):
        assert expected in tables


def test_create_and_get_document(store: SQLiteStore) -> None:
    doc_id = store.create_document(
        title="SOP",
        source_uri="/tmp/doc_001.md",
        mime_type="text/markdown",
        metadata={"network_type": "5g"},
        acl_tags=["wireless"],
        version="v1",
    )
    doc = store.get_document(doc_id)
    assert doc["title"] == "SOP"
    assert doc["parse_status"] == "uploaded"
    assert doc["chunk_count"] == 0
    assert doc["metadata"] == {"network_type": "5g"}
    assert doc["acl_tags"] == ["wireless"]
    assert doc["version"] == "v1"
    assert doc["parse_error"] is None


def test_transition_status_moves_uploaded_to_parsing(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    store.transition_status(doc_id, "parsing")
    assert store.get_document(doc_id)["parse_status"] == "parsing"


def test_mark_parse_failed_records_error(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    store.transition_status(doc_id, "parsing")
    store.mark_parse_failed(doc_id, parse_error="embed_unavailable", stage="embed")
    doc = store.get_document(doc_id)
    assert doc["parse_status"] == "parse_failed"
    assert "embed_unavailable" in doc["parse_error"]


def test_write_chunks_populates_chunks_and_fts(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        chunks=[
            {"chunk_id": f"chunk_{doc_id}_001", "chunk_no": 1, "content": "RRC 建立失败先查告警", "section_path": "root"},
            {"chunk_id": f"chunk_{doc_id}_002", "chunk_no": 2, "content": "传输误码先查光功率", "section_path": "root"},
        ],
        embeddings=[[0.1] * 8, [0.2] * 8],
        embedding_model="stub",
    )
    assert store.get_document(doc_id)["chunk_count"] == 2
    assert store.get_document(doc_id)["parse_status"] == "ready"
    listed = store.list_chunks(doc_id)
    assert len(listed) == 2
    assert listed[0]["embedding"] == [0.1] * 8


def test_publish_requires_legal_path(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    # uploaded -> published 非法
    with pytest.raises(Exception):
        store.transition_status(doc_id, "published")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": "c1", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    # write_chunks 已将状态推进到 ready；直接发布
    store.transition_status(doc_id, "published")
    assert store.get_document(doc_id)["parse_status"] == "published"


def test_get_unknown_document_raises_404(store: SQLiteStore) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        store.get_document("doc_unknown")
    assert exc.value.status_code == 404


def test_fts_search_returns_matching_chunk(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {"network_type": "5g"}, ["wireless"], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": "c1", "chunk_no": 1, "content": "RRC 建立失败先查告警", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    # FTS5 unicode61 把 "RRC" 作为独立 token；中文连续段整段为一个 token。
    # 用空格分隔的 ASCII token 验证召回。
    hits = store.search_fts("RRC", [doc_id])
    assert len(hits) == 1
    assert hits[0]["chunk_id"] == "c1"


def test_set_source_uri_updates_path(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    store.set_source_uri(doc_id, "/tmp/final.md")
    assert store.get_document(doc_id)["source_uri"] == "/tmp/final.md"
