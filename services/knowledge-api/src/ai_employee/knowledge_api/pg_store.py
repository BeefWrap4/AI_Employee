"""Postgres-backed knowledge store (R16-3).

Implements the same document + chunk lifecycle contract as
:class:`SQLiteStore`, but talks to PostgreSQL via the shared
:class:`ai_employee.common_schemas.db.DB` abstraction.  Selected by
:func:`build_knowledge_store` when ``DATABASE_URL`` points at Postgres.

BM25 keyword search on Postgres is delegated to OpenSearch (spec §5.4
mandates OpenSearch for BM25 in prod); the in-DB FTS5 index is a
SQLite-only fallback.  This store therefore exposes
:meth:`search_chunks_bm25` as a thin callback hook so the app can wire
in the OpenSearch provider without coupling this module to it.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ai_employee.common_schemas.db import DB, Backend, open_db
from ai_employee.common_schemas.errors import IndexCorruptedError
from ai_employee.common_schemas.security import (
    UnsafeSourceUriError,
    assert_safe_source_uri,
)
from fastapi import HTTPException, status

if TYPE_CHECKING:
    from ai_employee.knowledge_api.store import SQLiteStore

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    acl_tags_json TEXT NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status IN
        ('uploaded','parsing','parse_failed','ready','published','archived')),
    parse_error TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    version TEXT NOT NULL DEFAULT 'v1',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    chunk_no INTEGER NOT NULL,
    content TEXT NOT NULL,
    section_path TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    embedding_json TEXT,
    embedding_model TEXT,
    acl_tags_json TEXT NOT NULL DEFAULT '[]',
    table_id TEXT,
    row_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS qa_logs (
    qa_log_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT,
    question TEXT NOT NULL,
    knowledge_scopes_json TEXT NOT NULL DEFAULT '[]',
    rewritten_query TEXT,
    retrieved_chunks_json TEXT NOT NULL,
    answer TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    confidence REAL NOT NULL,
    latency_ms INTEGER NOT NULL,
    trace_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedbacks (
    feedback_id TEXT PRIMARY KEY,
    qa_log_id TEXT,
    trace_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    comment TEXT,
    user_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column_pg(db: DB, table: str, column: str, decl: str) -> None:
    """Add ``column`` to ``table`` if missing (mirrors SQLiteStore._ensure_column)."""
    row = db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? AND column_name = ?",
        (table, column),
    ).fetchone()
    if row is None:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


class PgKnowledgeStore:
    """Postgres-backed knowledge store (dual-backend with SQLiteStore)."""

    def __init__(self, *, db: DB, data_dir: str) -> None:
        self._db = db
        self.backend = db.backend
        self.data_dir = data_dir
        os.makedirs(os.path.join(data_dir, "raw"), exist_ok=True)
        self._lock = threading.Lock()
        self._bm25_search_fn: Callable[..., list[dict[str, Any]]] | None = None

    # -- wiring ---------------------------------------------------------------
    def set_bm25_search(self, fn: Callable[..., list[dict[str, Any]]]) -> None:
        """Inject the OpenSearch BM25 search callback (spec §5.4 prod path)."""
        self._bm25_search_fn = fn

    # -- schema ---------------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            for stmt in _SCHEMA_PG.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._db.execute(stmt)
            self._db.commit()

    def list_tables(self) -> list[str]:
        if self.backend == Backend.POSTGRES:
            rows = self._db.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = current_schema()",
            ).fetchall()
        else:
            # SQLite portable path (PgKnowledgeStore can run on SQLite too).
            rows = self._db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'",
            ).fetchall()
        return [r["name"] for r in rows]

    # -- documents ------------------------------------------------------------
    def create_document(
        self,
        title: str,
        source_uri: str,
        mime_type: str,
        metadata: dict[str, Any],
        acl_tags: list[str],
        version: str,
    ) -> str:
        try:
            assert_safe_source_uri(source_uri, self.data_dir)
        except UnsafeSourceUriError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "unsafe_source_uri", "message": str(exc)},
            ) from exc
        with self._lock:
            count_row = self._db.execute(
                "SELECT COUNT(*) AS c FROM documents",
            ).fetchone()
            count = int(count_row["c"] if count_row else 0)
            doc_id = f"doc_{count + 1:03d}"
            now = _now()
            self._db.execute(
                """INSERT INTO documents
                   (doc_id, title, source_uri, mime_type, metadata_json, acl_tags_json,
                    parse_status, parse_error, chunk_count, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,?, 'uploaded', NULL, 0, ?, ?, ?)""",
                (
                    doc_id, title, source_uri, mime_type,
                    json.dumps(metadata, ensure_ascii=False),
                    json.dumps(acl_tags, ensure_ascii=False),
                    version, now, now,
                ),
            )
            self._db.commit()
            return doc_id

    def get_document(self, doc_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "document_not_found", "doc_id": doc_id},
            )
        return _document_row_to_dict(row)

    def list_documents(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM documents ORDER BY created_at",
        ).fetchall()
        return [_document_row_to_dict(r) for r in rows]

    def update_parse_status(
        self, doc_id: str, target: str, chunk_count: int | None = None,
    ) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT parse_status FROM documents WHERE doc_id = ?", (doc_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "document_not_found", "doc_id": doc_id},
            )
        transition_parse_status(row["parse_status"], target)
        if chunk_count is not None:
            self._db.execute(
                "UPDATE documents SET parse_status = ?, chunk_count = ?, "
                "updated_at = ? WHERE doc_id = ?",
                (target, chunk_count, _now(), doc_id),
            )
        else:
            self._db.execute(
                "UPDATE documents SET parse_status = ?, updated_at = ? WHERE doc_id = ?",
                (target, _now(), doc_id),
            )
        self._db.commit()
        return self.get_document(doc_id)

    # -- chunks ---------------------------------------------------------------
    def create_chunk(
        self,
        *,
        doc_id: str,
        chunk_no: int,
        content: str,
        section_path: str,
        page_no: int = 1,
        acl_tags: list[str] | None = None,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
        table_id: str | None = None,
        row_id: str | None = None,
    ) -> str:
        with self._lock:
            self._db.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE doc_id = ?", (doc_id,),
            ).fetchone()
            chunk_id = f"{doc_id}_chunk_{chunk_no:03d}"
            acl_json = json.dumps(acl_tags or [], ensure_ascii=False)
            emb_json = json.dumps(embedding, ensure_ascii=False) if embedding else None
            self._db.execute(
                """INSERT INTO chunks
                   (chunk_id, doc_id, chunk_no, content, section_path, page_no,
                    embedding_json, embedding_model, acl_tags_json,
                    table_id, row_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, ?, ?, ?)""",
                (
                    chunk_id, doc_id, chunk_no, content, section_path, page_no,
                    emb_json, embedding_model, acl_json,
                    table_id, row_id, _now(),
                ),
            )
            self._db.commit()
            return chunk_id

    def get_chunks_for_doc(self, doc_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM chunks WHERE doc_id = ? ORDER BY chunk_no",
            (doc_id,),
        ).fetchall()
        return [_chunk_row_to_dict(r) for r in rows]

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,),
        ).fetchone()
        return _chunk_row_to_dict(row) if row else None

    # -- BM25 search (delegated) ---------------------------------------------
    def search_chunks_bm25(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """BM25 keyword search.

        On Postgres this delegates to the injected OpenSearch callback
        (spec §5.4 prod path).  When no callback is wired, raises
        :class:`IndexCorruptedError` so the caller can fall back to
        vector-only retrieval rather than silently returning nothing.
        """
        if self._bm25_search_fn is None:
            raise IndexCorruptedError(
                "Postgres backend has no BM25 search callback; "
                "wire OpenSearch via set_bm25_search()",
            )
        return self._bm25_search_fn(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Row mappers (dict rows from DB row_factory='dict')
# --------------------------------------------------------------------------- #


def _document_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": row["doc_id"],
        "title": row["title"],
        "source_uri": row["source_uri"],
        "mime_type": row["mime_type"],
        "metadata": json.loads(row["metadata_json"]) if row.get("metadata_json") else {},
        "acl_tags": json.loads(row["acl_tags_json"]) if row.get("acl_tags_json") else [],
        "parse_status": row["parse_status"],
        "parse_error": row.get("parse_error"),
        "chunk_count": int(row.get("chunk_count") or 0),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _chunk_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": row["chunk_id"],
        "doc_id": row["doc_id"],
        "chunk_no": int(row["chunk_no"]),
        "content": row["content"],
        "section_path": row["section_path"],
        "page_no": int(row.get("page_no") or 1),
        "embedding": json.loads(row["embedding_json"]) if row.get("embedding_json") else None,
        "embedding_model": row.get("embedding_model"),
        "acl_tags": json.loads(row["acl_tags_json"]) if row.get("acl_tags_json") else [],
        "table_id": row.get("table_id"),
        "row_id": row.get("row_id"),
        "created_at": row["created_at"],
    }


def transition_parse_status(current: str, target: str) -> None:
    """Validate the parse-status transition (mirrors SQLiteStore)."""
    if current == target:
        return
    allowed = {
        "uploaded": {"parsing", "parse_failed", "ready", "archived"},
        "parsing": {"parse_failed", "ready"},
        "parse_failed": {"parsing", "archived"},
        "ready": {"published", "archived"},
        "published": {"archived"},
        "archived": set(),
    }
    if target not in allowed.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "invalid_status_transition",
                "current": current,
                "target": target,
            },
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_knowledge_store(
    *,
    db_path: str | None = None,
    data_dir: str = "./var/data",
    database_url: str | None = None,
) -> SQLiteStore | PgKnowledgeStore:
    """Pick SQLiteStore or PgKnowledgeStore based on DATABASE_URL.

    Defaults to :class:`SQLiteStore` (the existing, unchanged path) so
    dev/test behaviour is identical when ``DATABASE_URL`` is unset.
    """
    from ai_employee.common_schemas.db import detect_backend

    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    backend = detect_backend(url)
    if backend == Backend.POSTGRES:
        db = open_db(url, row_factory="dict")
        store = PgKnowledgeStore(db=db, data_dir=data_dir)
        store.init_schema()
        return store
    # SQLite (default) — unchanged existing path.
    from ai_employee.knowledge_api.store import SQLiteStore

    return SQLiteStore(
        db_path=db_path or os.path.join(data_dir, "knowledge.sqlite3"),
        data_dir=data_dir,
    )


__all__ = [
    "PgKnowledgeStore",
    "build_knowledge_store",
]
