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


def _make_qa_log(store: SQLiteStore, trace_id: str, session: str, question: str,
                  chunks: list, answer: str = "A") -> None:
    store.write_qa_log(
        qa_log_id=f"qa_{trace_id}",
        session_id=session,
        question=question,
        retrieved_chunks=chunks,
        answer=answer,
        model_name="m1-template",
        prompt_version="v1",
        confidence=0.7,
        latency_ms=100,
        trace_id=trace_id,
    )


def test_list_qa_logs_filters_by_session(store: SQLiteStore) -> None:
    _make_qa_log(store, "t_001", "sA", "q1", [{"chunk_id": "c1", "doc_id": "d1"}])
    _make_qa_log(store, "t_002", "sB", "q2", [{"chunk_id": "c2", "doc_id": "d2"}])
    _make_qa_log(store, "t_003", "sA", "q3", [])
    items, total = store.list_qa_logs(session_id="sA")
    assert total == 2
    assert {i["trace_id"] for i in items} == {"t_001", "t_003"}


def test_list_qa_logs_pagination(store: SQLiteStore) -> None:
    for i in range(5):
        _make_qa_log(store, f"t_{i:03d}", "sX", f"q{i}", [])
    items, total = store.list_qa_logs(page=1, page_size=2)
    assert total == 5
    assert len(items) == 2
    items, total = store.list_qa_logs(page=2, page_size=2)
    assert total == 5
    assert len(items) == 2
    items, total = store.list_qa_logs(page=3, page_size=2)
    assert len(items) == 1


def test_get_qa_log_returns_full_with_retrieved_chunks(store: SQLiteStore) -> None:
    _make_qa_log(store, "t_xyz", "sA", "Q", [{"chunk_id": "c1", "doc_id": "d1"}])
    log = store.get_qa_log("t_xyz")
    assert log is not None
    assert log["trace_id"] == "t_xyz"
    assert log["retrieved_chunks"] == [{"chunk_id": "c1", "doc_id": "d1"}]
    assert log["question"] == "Q"


def test_get_qa_log_returns_none_for_missing(store: SQLiteStore) -> None:
    assert store.get_qa_log("nonexistent") is None


def test_list_feedbacks_filters_by_trace_id(store: SQLiteStore) -> None:
    fid1 = store.write_feedback("t_a", "useful", "ok")
    fid2 = store.write_feedback("t_b", "useless", "nope")
    items, total = store.list_feedbacks(trace_id="t_a")
    assert total == 1
    assert items[0]["feedback_id"] == fid1
    assert items[0]["feedback_type"] == "useful"
    assert items[0]["comment"] == "ok"


def test_list_feedbacks_pagination_and_filter(store: SQLiteStore) -> None:
    for i in range(5):
        store.write_feedback("t_x", "useful", f"c{i}")
    items, total = store.list_feedbacks(trace_id="t_x", page=1, page_size=2)
    assert total == 5
    assert len(items) == 2


def test_list_documents_filters_by_status(store: SQLiteStore) -> None:
    d1 = store.create_document("Pub", "/tmp/p", "text/plain", {}, ["a"], "v1")
    store.transition_status(d1, "parsing")
    store.write_chunks(d1, [{"chunk_id": f"c_{d1}", "chunk_no": 1, "content": "x", "section_path": "root"}], [[0.0] * 8], "stub")
    store.transition_status(d1, "ready")
    store.transition_status(d1, "published")
    d2 = store.create_document("Up", "/tmp/u", "text/plain", {}, ["a"], "v1")
    items, total = store.list_documents(status="published")
    assert total == 1
    assert items[0]["doc_id"] == d1
    items, total = store.list_documents()
    assert total == 2
