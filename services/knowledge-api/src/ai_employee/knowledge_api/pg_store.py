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
import logging
import os
import re
import threading
import uuid
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

_LOG = logging.getLogger(__name__)
_WARNED_FALLBACK = False

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
        # R30-A: derive doc_id from a uuid4 suffix (8 hex chars) so concurrent
        # writers on the same PG backend never collide on the PK.  Pre-R30
        # the scheme was ``doc_{COUNT(*)+1:03d}`` — under multiple FastAPI
        # workers / replicas that race and produce a UniqueViolation / 500.
        # The uuid4 collision probability for 32 bits is ~1e-9 per pair,
        # which is negligible at the rates the knowledge-api actually serves.
        doc_id = f"doc_{uuid.uuid4().hex[:8]}"
        with self._lock:
            now = _now()
            self._db.execute(
                """INSERT INTO documents
                   (doc_id, title, source_uri, mime_type, metadata_json, acl_tags_json,
                    parse_status, parse_error, chunk_count, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,?, 'uploaded', NULL, 0, ?, ?, ?)""",
                (
                    doc_id,
                    title,
                    source_uri,
                    mime_type,
                    json.dumps(metadata, ensure_ascii=False),
                    json.dumps(acl_tags, ensure_ascii=False),
                    version,
                    now,
                    now,
                ),
            )
            self._db.commit()
            return doc_id

    def get_document(self, doc_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "document_not_found", "doc_id": doc_id},
            )
        return _document_row_to_dict(row)

    def set_source_uri(self, doc_id: str, source_uri: str) -> None:
        """Update ``source_uri`` for an uploaded document (PG backend).

        Mirrors :meth:`SQLiteStore.set_source_uri` so the upload flow
        in ``app.create_document`` works unchanged on the PG path.
        Validates the path is under ``data_dir`` before persisting.
        """
        try:
            assert_safe_source_uri(source_uri, self.data_dir)
        except UnsafeSourceUriError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error_code": "path_not_allowed", "message": str(exc)},
            ) from exc
        self._db.execute(
            "UPDATE documents SET source_uri = ?, updated_at = ? WHERE doc_id = ?",
            (source_uri, _now(), doc_id),
        )
        self._db.commit()

    def update_parse_status(
        self,
        doc_id: str,
        target: str,
        chunk_count: int | None = None,
    ) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT parse_status FROM documents WHERE doc_id = ?",
            (doc_id,),
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

    def transition_status(self, doc_id: str, target: str) -> dict[str, Any]:
        """Validate + apply a parse-status transition (PG mirror of SQLiteStore).

        R30-A: previously only ``update_parse_status`` was exposed, which
        hard-coded chunk_count semantics.  The ingestion worker / app
        routes call ``transition_status`` on PG when DATASETS_URL is
        set — fill the gap.
        """
        return self.update_parse_status(doc_id, target)

    def mark_parse_failed(self, doc_id: str, parse_error: str, stage: str) -> None:
        """Mark a document ``parse_failed`` with the offending stage (PG).

        Mirrors :meth:`SQLiteStore.mark_parse_failed`.  Skips transition
        validation when the document is already in ``parse_failed``.
        """
        row = self._db.execute(
            "SELECT parse_status FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "document_not_found", "doc_id": doc_id},
            )
        current = row["parse_status"]
        if current != "parse_failed":
            transition_parse_status(current, "parse_failed")
        self._db.execute(
            "UPDATE documents SET parse_status = 'parse_failed', "
            "parse_error = ?, updated_at = ? WHERE doc_id = ?",
            (f"[{stage}] {parse_error}", _now(), doc_id),
        )
        self._db.commit()

    def write_chunks(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
        embedding_model: str,
        acl_tags_override: list[str] | None = None,
    ) -> None:
        """Bulk-insert chunks + flip the document to ``ready`` (PG).

        Mirrors :meth:`SQLiteStore.write_chunks`.  ``acl_tags_override``
        follows the same empty-list-means-inherit convention as the
        SQLite path so the chunk-level ACL filter behaves identically.
        """
        row = self._db.execute(
            "SELECT parse_status FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "document_not_found", "doc_id": doc_id},
            )
        if acl_tags_override is None:
            acl_tags: list[str] = []
        else:
            acl_tags = list(acl_tags_override)
        acl_json = json.dumps(acl_tags, ensure_ascii=False)
        now = _now()
        for chunk, vec in zip(chunks, embeddings, strict=False):
            self._db.execute(
                """INSERT INTO chunks
                   (chunk_id, doc_id, chunk_no, content, section_path, page_no,
                    embedding_json, embedding_model, acl_tags_json,
                    table_id, row_id, created_at)
                   VALUES (?,?,?,?,?, 1, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk["chunk_id"],
                    doc_id,
                    chunk["chunk_no"],
                    chunk["content"],
                    chunk["section_path"],
                    json.dumps(vec, ensure_ascii=False),
                    embedding_model,
                    acl_json,
                    chunk.get("table_id"),
                    chunk.get("row_id"),
                    now,
                ),
            )
        current = row["parse_status"]
        if current != "ready":
            transition_parse_status(current, "ready")
        self._db.execute(
            "UPDATE documents SET chunk_count = ?, parse_status = 'ready', "
            "parse_error = NULL, updated_at = ? WHERE doc_id = ?",
            (len(chunks), now, doc_id),
        )
        self._db.commit()

    def write_qa_log(self, **fields: Any) -> None:
        """Insert a qa_logs row (PG mirror of SQLiteStore.write_qa_log)."""
        self._db.execute(
            """INSERT INTO qa_logs
               (qa_log_id, session_id, user_id, question, rewritten_query,
                knowledge_scopes_json, retrieved_chunks_json, answer, model_name, prompt_version,
                confidence, latency_ms, trace_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, ?)""",
            (
                fields["qa_log_id"],
                fields["session_id"],
                fields.get("user_id"),
                fields["question"],
                fields.get("rewritten_query"),
                json.dumps(fields.get("knowledge_scopes", []), ensure_ascii=False),
                json.dumps(fields["retrieved_chunks"], ensure_ascii=False),
                fields["answer"],
                fields["model_name"],
                fields["prompt_version"],
                fields["confidence"],
                fields["latency_ms"],
                fields["trace_id"],
                _now(),
            ),
        )
        self._db.commit()

    def write_feedback(
        self, trace_id: str, feedback_type: str, comment: str | None,
    ) -> str:
        """Insert a feedbacks row + return its id (PG mirror of SQLiteStore)."""
        feedback_id = f"fb_{uuid.uuid4().hex[:8]}"
        self._db.execute(
            """INSERT INTO feedbacks
               (feedback_id, qa_log_id, trace_id, feedback_type, comment, user_id, created_at)
               VALUES (?, NULL, ?, ?, ?, NULL, ?)""",
            (feedback_id, trace_id, feedback_type, comment, _now()),
        )
        self._db.commit()
        return feedback_id

    def list_qa_logs(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated qa_logs query (PG mirror of SQLiteStore.list_qa_logs)."""
        where: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("created_at < ?")
            params.append(until)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size
        total_row = self._db.execute(
            f"SELECT COUNT(*) AS c FROM qa_logs{where_sql}", params,
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = self._db.execute(
            f"""SELECT qa_log_id, trace_id, session_id, user_id, question, answer,
                knowledge_scopes_json, confidence, latency_ms, model_name,
                prompt_version, created_at
                FROM qa_logs{where_sql} ORDER BY created_at DESC, qa_log_id DESC
                LIMIT ? OFFSET ?""",
            [*params, page_size, offset],
        ).fetchall()
        return [_qa_log_summary_row(r) for r in rows], total

    def get_qa_log(self, trace_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM qa_logs WHERE trace_id = ?",
            (trace_id,),
        ).fetchone()
        if row is None:
            return None
        item = _qa_log_summary_row(row)
        item["retrieved_chunks"] = json.loads(row["retrieved_chunks_json"])
        return item

    def list_feedbacks(
        self,
        *,
        trace_id: str | None = None,
        feedback_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated feedbacks query (PG mirror of SQLiteStore.list_feedbacks)."""
        where: list[str] = []
        params: list[Any] = []
        if trace_id is not None:
            where.append("trace_id = ?")
            params.append(trace_id)
        if feedback_type is not None:
            where.append("feedback_type = ?")
            params.append(feedback_type)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("created_at < ?")
            params.append(until)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size
        total_row = self._db.execute(
            f"SELECT COUNT(*) AS c FROM feedbacks{where_sql}", params,
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = self._db.execute(
            f"""SELECT feedback_id, qa_log_id, trace_id, feedback_type, comment,
                user_id, created_at FROM feedbacks{where_sql}
                ORDER BY created_at DESC, feedback_id DESC LIMIT ? OFFSET ?""",
            [*params, page_size, offset],
        ).fetchall()
        return [dict(r) for r in rows], total

    def list_documents(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated documents query (PG mirror of SQLiteStore.list_documents).

        Always returns ``(items, total)`` — matches the SQLite signature so
        the existing admin endpoint works on both backends.  The legacy
        PG code path that returned just the list is exposed as
        :meth:`list_documents_unpaginated` for callers that still want it.
        """
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("parse_status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size
        total_row = self._db.execute(
            f"SELECT COUNT(*) AS c FROM documents{where_sql}", params,
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = self._db.execute(
            f"SELECT * FROM documents{where_sql} "
            f"ORDER BY created_at DESC, doc_id DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return [_document_row_to_dict(r) for r in rows], total

    def list_documents_unpaginated(
        self, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Full (unpaginated) list of documents — legacy admin contract."""
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("parse_status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._db.execute(
            f"SELECT * FROM documents{where_sql} ORDER BY created_at",
            params,
        ).fetchall()
        return [_document_row_to_dict(r) for r in rows] if rows else []  # type: ignore[return-value]  # always returns list when sqlite fallback

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
                "SELECT COUNT(*) AS c FROM chunks WHERE doc_id = ?",
                (doc_id,),
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
                    chunk_id,
                    doc_id,
                    chunk_no,
                    content,
                    section_path,
                    page_no,
                    emb_json,
                    embedding_model,
                    acl_json,
                    table_id,
                    row_id,
                    _now(),
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
            "SELECT * FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        return _chunk_row_to_dict(row) if row else None

    def search_fts(
        self,
        query: str,
        doc_ids: list[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Portable keyword fallback used when OpenSearch is disabled.

        SQLiteStore uses an FTS5 virtual table.  The Postgres-backed MVP
        keeps BM25 in OpenSearch, but local Docker defaults it off, so this
        lightweight scorer preserves the RAG flow for docker-compose.
        """
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        rows = self._db.execute(
            f"SELECT c.chunk_id, c.doc_id, c.chunk_no, c.content, c.section_path, "
            f"c.embedding_json, c.embedding_model, c.acl_tags_json, d.title "
            f"FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
            f"WHERE c.doc_id IN ({placeholders})",
            doc_ids,
        ).fetchall()
        terms = _keyword_terms(query)
        if not terms:
            return []

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for row in rows:
            haystack = " ".join(
                str(row.get(field) or "")
                for field in ("title", "section_path", "content")
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score <= 0:
                continue
            item = dict(row)
            item["acl_tags"] = (
                json.loads(row["acl_tags_json"]) if row.get("acl_tags_json") else []
            )
            scored.append((score, -int(row.get("chunk_no") or 0), item))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item for _score, _chunk_no, item in scored[:limit]]

    def list_chunks_for_vector_recall(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        rows = self._db.execute(
            f"SELECT chunk_id, doc_id, content, section_path, embedding_json, "
            f"embedding_model, acl_tags_json "
            f"FROM chunks WHERE doc_id IN ({placeholders}) AND embedding_json IS NOT NULL",
            doc_ids,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["embedding"] = (
                json.loads(row["embedding_json"]) if row.get("embedding_json") else None
            )
            item["acl_tags"] = (
                json.loads(row["acl_tags_json"]) if row.get("acl_tags_json") else []
            )
            out.append(item)
        return out

    def get_doc_title(self, doc_id: str) -> str:
        row = self._db.execute(
            "SELECT title FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        return row["title"] if row else ""

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


def _qa_log_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "qa_log_id": row["qa_log_id"],
        "trace_id": row["trace_id"],
        "session_id": row["session_id"],
        "user_id": row.get("user_id"),
        "question": row["question"],
        "knowledge_scopes": json.loads(row["knowledge_scopes_json"] or "[]"),
        "answer": row["answer"],
        "confidence": row["confidence"],
        "latency_ms": row["latency_ms"],
        "model_name": row["model_name"],
        "prompt_version": row["prompt_version"],
        "created_at": row["created_at"],
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


def _keyword_terms(query: str) -> list[str]:
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", query)]
    return list(dict.fromkeys(t for t in terms if len(t) >= 2))


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

    R29-A: when ``DATABASE_URL`` is unset and the SQLite fallback is
    chosen, emit a one-shot deprecation warning so operators running
    the production chart can see they're on the legacy default.
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
    global _WARNED_FALLBACK  # module-level throttle
    if not _WARNED_FALLBACK:
        _WARNED_FALLBACK = True
        _LOG.warning(
            "knowledge-api: DATABASE_URL is unset; falling back to local SQLite "
            "store. Set DATABASE_URL=postgresql://... for production.",
        )
    from ai_employee.knowledge_api.store import SQLiteStore

    return SQLiteStore(
        db_path=db_path or os.path.join(data_dir, "knowledge.sqlite3"),
        data_dir=data_dir,
    )


__all__ = [
    "PgKnowledgeStore",
    "build_knowledge_store",
]
