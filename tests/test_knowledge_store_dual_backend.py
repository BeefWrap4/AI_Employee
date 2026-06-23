"""knowledge-api dual-backend store contract tests (R16-3).

:class:`PgKnowledgeStore` runs the same SQL against any backend via the
shared :class:`DB` abstraction.  We prove portability by running its
document + chunk lifecycle against:

* a SQLite DB (always available — proves the SQL is dialect-portable), and
* a live PostgreSQL DB (when ``TEST_POSTGRES_URL`` is set — proves the
  spec-mandated prod path).

The factory :func:`build_knowledge_store` picks :class:`SQLiteStore`
(default, unchanged) or :class:`PgKnowledgeStore` (Postgres).
"""

from __future__ import annotations

import os

import pytest
from ai_employee.common_schemas.db import Backend, open_db
from ai_employee.knowledge_api.pg_store import PgKnowledgeStore
from ai_employee.knowledge_api.store import (
    SQLiteStore,
    build_knowledge_store,
)

# --------------------------------------------------------------------------- #
# DB fixtures: one SQLite (always) + one PG (when available)
# --------------------------------------------------------------------------- #


def _sqlite_db(tmp_path):
    return open_db(f"sqlite:///{tmp_path}/k.sqlite3", row_factory="dict")


def _pg_db():
    return open_db(os.environ["TEST_POSTGRES_URL"], row_factory="dict")


def _dbs(tmp_path):
    yield "sqlite", _sqlite_db(tmp_path)
    if os.getenv("TEST_POSTGRES_URL"):
        pg = _pg_db()
        # R30-A: list_documents now returns (items, total) — clean shared
        # PG tables so per-test counts aren't polluted by prior runs.
        _truncate_knowledge_tables(pg)
        yield "postgres", pg


def _store(db, tmp_path):
    s = PgKnowledgeStore(db=db, data_dir=str(tmp_path))
    s.init_schema()
    return s


def _truncate_knowledge_tables(db) -> None:
    """Wipe shared PG knowledge tables so each PG-leg test starts clean."""
    if db.backend != Backend.POSTGRES:
        return
    db.execute("DELETE FROM chunks")
    db.execute("DELETE FROM documents")
    db.commit()


def _safe_uri(tmp_path, name: str) -> str:
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    return str(raw / name)


# --------------------------------------------------------------------------- #
# Factory selection
# --------------------------------------------------------------------------- #


def test_build_knowledge_store_defaults_to_sqlite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = build_knowledge_store(
        db_path=str(tmp_path / "k.sqlite3"),
        data_dir=str(tmp_path),
    )
    assert isinstance(store, SQLiteStore)


def test_build_knowledge_store_sqlite_url(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/k.sqlite3")
    store = build_knowledge_store(data_dir=str(tmp_path))
    assert isinstance(store, SQLiteStore)


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_build_knowledge_store_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
    store = build_knowledge_store(data_dir="./var/data")
    assert isinstance(store, PgKnowledgeStore)
    assert store.backend == Backend.POSTGRES


# --------------------------------------------------------------------------- #
# Contract: document + chunk lifecycle, portable across backends
# --------------------------------------------------------------------------- #


def test_document_create_get_round_trip(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        doc_id = store.create_document(
            title="RCA 手册",
            source_uri=_safe_uri(tmp_path, "rca.pdf"),
            mime_type="application/pdf",
            metadata={"author": "ops"},
            acl_tags=["ops-team"],
            version="v1",
        )
        assert doc_id.startswith("doc_")
        doc = store.get_document(doc_id)
        assert doc["title"] == "RCA 手册"
        assert doc["parse_status"] == "uploaded"
        assert doc["version"] == "v1"


def test_document_not_found_raises(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        with pytest.raises(Exception):
            store.get_document("doc_does_not_exist")


def test_list_documents_returns_created(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        store.create_document(
            title="a",
            source_uri=_safe_uri(tmp_path, "a"),
            mime_type="text/plain",
            metadata={},
            acl_tags=[],
            version="v1",
        )
        store.create_document(
            title="b",
            source_uri=_safe_uri(tmp_path, "b"),
            mime_type="text/plain",
            metadata={},
            acl_tags=[],
            version="v1",
        )
        # R30-A: list_documents is now paginated (matches SQLiteStore);
        # callers that want the full list pass a large page_size.
        docs, total = store.list_documents(page=1, page_size=100)
        assert total == 2
        titles = {d["title"] for d in docs}
        assert {"a", "b"}.issubset(titles)


def test_create_chunk_and_retrieve(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        doc_id = store.create_document(
            title="x",
            source_uri=_safe_uri(tmp_path, "x"),
            mime_type="text/plain",
            metadata={},
            acl_tags=[],
            version="v1",
        )
        store.create_chunk(
            doc_id=doc_id,
            chunk_no=0,
            content="RRC 连接失败根因分析",
            section_path="root",
            page_no=1,
            acl_tags=["ops-team"],
            embedding=None,
            embedding_model="bge-m3",
        )
        chunks = store.get_chunks_for_doc(doc_id)
        assert len(chunks) == 1
        assert chunks[0]["content"] == "RRC 连接失败根因分析"
        assert chunks[0]["section_path"] == "root"


def test_pg_store_keyword_and_vector_recall_methods(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        doc_id = store.create_document(
            title="RRC troubleshooting",
            source_uri=_safe_uri(tmp_path, "rrc.md"),
            mime_type="text/markdown",
            metadata={},
            acl_tags=[],
            version="v1",
        )
        store.create_chunk(
            doc_id=doc_id,
            chunk_no=0,
            content="RRC setup failure rate rises: check alarms, KPI, and parameter changes.",
            section_path="Access failure > RRC setup failure",
            page_no=1,
            acl_tags=[],
            embedding=[1.0, 0.0, 0.0],
            embedding_model="stub",
        )

        keyword_hits = store.search_fts("RRC setup failure", [doc_id], limit=5)
        assert len(keyword_hits) == 1
        assert keyword_hits[0]["doc_id"] == doc_id
        assert keyword_hits[0]["title"] == "RRC troubleshooting"

        vector_rows = store.list_chunks_for_vector_recall([doc_id])
        assert len(vector_rows) == 1
        assert vector_rows[0]["embedding"] == [1.0, 0.0, 0.0]
        assert store.get_doc_title(doc_id) == "RRC troubleshooting"


def test_update_parse_status(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        doc_id = store.create_document(
            title="x",
            source_uri=_safe_uri(tmp_path, "x"),
            mime_type="text/plain",
            metadata={},
            acl_tags=[],
            version="v1",
        )
        store.update_parse_status(doc_id, "ready", chunk_count=3)
        doc = store.get_document(doc_id)
        assert doc["parse_status"] == "ready"
        assert doc["chunk_count"] == 3


def test_list_tables_returns_core_tables(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        tables = set(store.list_tables())
        assert {"documents", "chunks"}.issubset(tables)


def test_invalid_status_transition_rejected(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = _store(db, tmp_path)
        doc_id = store.create_document(
            title="x",
            source_uri=_safe_uri(tmp_path, "x"),
            mime_type="text/plain",
            metadata={},
            acl_tags=[],
            version="v1",
        )
        # uploaded -> published is not a valid transition.
        with pytest.raises(Exception):
            store.update_parse_status(doc_id, "published")


# --------------------------------------------------------------------------- #
# BM25 delegation
# --------------------------------------------------------------------------- #


def test_bm25_search_raises_without_callback(tmp_path) -> None:
    from ai_employee.common_schemas.errors import IndexCorruptedError

    db = _sqlite_db(tmp_path)
    store = _store(db, tmp_path)
    with pytest.raises(IndexCorruptedError):
        store.search_chunks_bm25("query")


def test_bm25_search_delegates_to_callback(tmp_path) -> None:
    db = _sqlite_db(tmp_path)
    store = _store(db, tmp_path)
    captured: dict = {}

    def fake_search(query: str, **kw):
        captured["query"] = query
        return [{"chunk_id": "c1", "content": "hit"}]

    store.set_bm25_search(fake_search)
    out = store.search_chunks_bm25("RRC failure")
    assert out == [{"chunk_id": "c1", "content": "hit"}]
    assert captured["query"] == "RRC failure"


# --------------------------------------------------------------------------- #
# Portability proof: PgKnowledgeStore runs on a SQLite DB
# --------------------------------------------------------------------------- #


def test_pg_store_runs_against_sqlite_db(tmp_path) -> None:
    """The PG store's SQL is dialect-portable: it works on SQLite too."""
    db = _sqlite_db(tmp_path)
    store = PgKnowledgeStore(db=db, data_dir=str(tmp_path))
    assert store.backend == Backend.SQLITE
    store.init_schema()
    doc_id = store.create_document(
        title="t",
        source_uri=_safe_uri(tmp_path, "t"),
        mime_type="text/plain",
        metadata={},
        acl_tags=[],
        version="v1",
    )
    assert store.get_document(doc_id)["title"] == "t"
    store.create_chunk(
        doc_id=doc_id,
        chunk_no=0,
        content="c",
        section_path="root",
    )
    assert len(store.get_chunks_for_doc(doc_id)) == 1


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_pg_store_runs_against_live_postgres(tmp_path) -> None:
    db = _pg_db()
    store = PgKnowledgeStore(db=db, data_dir="./var/data")
    assert store.backend == Backend.POSTGRES
    store.init_schema()
    doc_id = store.create_document(
        title="pg-doc",
        source_uri=os.path.abspath("./var/data/raw/pg-doc"),
        mime_type="text/plain",
        metadata={},
        acl_tags=[],
        version="v1",
    )
    assert store.get_document(doc_id)["title"] == "pg-doc"
