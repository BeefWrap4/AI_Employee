from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from ai_employee.common_schemas.knowledge import DocumentStatus

_SCHEMA = """
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
    created_at TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    section_path,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(chunk_id, content, section_path)
    VALUES (new.chunk_id, new.content, new.section_path);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
END;

CREATE TABLE IF NOT EXISTS qa_logs (
    qa_log_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT,
    question TEXT NOT NULL,
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
"""

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    DocumentStatus.UPLOADED.value: {DocumentStatus.PARSING.value},
    DocumentStatus.PARSING.value: {
        DocumentStatus.READY.value,
        DocumentStatus.PARSE_FAILED.value,
    },
    DocumentStatus.PARSE_FAILED.value: {DocumentStatus.UPLOADED.value},
    DocumentStatus.READY.value: {DocumentStatus.PUBLISHED.value},
    DocumentStatus.PUBLISHED.value: {DocumentStatus.ARCHIVED.value},
    DocumentStatus.ARCHIVED.value: {DocumentStatus.PUBLISHED.value},
}


class IllegalTransitionError(Exception):
    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal transition {current} -> {target}")


def transition_parse_status(current: str, target: str) -> str:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise IllegalTransitionError(current, target)
    return target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    def __init__(self, db_path: str, data_dir: str) -> None:
        self.db_path = db_path
        self.data_dir = data_dir
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(os.path.join(data_dir, "raw"), exist_ok=True)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def list_tables(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return [r["name"] for r in rows]

    def create_document(
        self,
        title: str,
        source_uri: str,
        mime_type: str,
        metadata: dict[str, Any],
        acl_tags: list[str],
        version: str,
    ) -> str:
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
            doc_id = f"doc_{count + 1:03d}"
            now = _now()
            conn.execute(
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
            conn.commit()
            return doc_id

    def get_document(self, doc_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "document_not_found", "doc_id": doc_id},
            )
        return _document_row_to_dict(row)

    def set_source_uri(self, doc_id: str, source_uri: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE documents SET source_uri = ?, updated_at = ? WHERE doc_id = ?",
                (source_uri, _now(), doc_id),
            )
            conn.commit()

    def transition_status(self, doc_id: str, target: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT parse_status FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error_code": "document_not_found", "doc_id": doc_id},
                )
            transition_parse_status(row["parse_status"], target)
            conn.execute(
                "UPDATE documents SET parse_status = ?, updated_at = ? WHERE doc_id = ?",
                (target, _now(), doc_id),
            )
            conn.commit()
        return self.get_document(doc_id)

    def mark_parse_failed(self, doc_id: str, parse_error: str, stage: str) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT parse_status FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error_code": "document_not_found", "doc_id": doc_id},
                )
            if row["parse_status"] != DocumentStatus.PARSE_FAILED.value:
                transition_parse_status(row["parse_status"], DocumentStatus.PARSE_FAILED.value)
            conn.execute(
                "UPDATE documents SET parse_status = 'parse_failed', "
                "parse_error = ?, updated_at = ? WHERE doc_id = ?",
                (f"[{stage}] {parse_error}", _now(), doc_id),
            )
            conn.commit()

    def write_chunks(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
        embedding_model: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT parse_status FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error_code": "document_not_found", "doc_id": doc_id},
                )
            now = _now()
            for chunk, vec in zip(chunks, embeddings):
                conn.execute(
                    """INSERT INTO chunks
                       (chunk_id, doc_id, chunk_no, content, section_path, page_no,
                        embedding_json, embedding_model, created_at)
                       VALUES (?,?,?,?,?, 1, ?, ?, ?)""",
                    (
                        chunk["chunk_id"],
                        doc_id,
                        chunk["chunk_no"],
                        chunk["content"],
                        chunk["section_path"],
                        json.dumps(vec, ensure_ascii=False),
                        embedding_model,
                        now,
                    ),
                )
            transition = row["parse_status"]
            if transition != DocumentStatus.READY.value:
                transition_parse_status(transition, DocumentStatus.READY.value)
            conn.execute(
                "UPDATE documents SET chunk_count = ?, parse_status = 'ready', "
                "parse_error = NULL, updated_at = ? WHERE doc_id = ?",
                (len(chunks), now, doc_id),
            )
            conn.commit()

    def list_chunks(self, doc_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE doc_id = ? ORDER BY chunk_no", (doc_id,)
            ).fetchall()
        return [_chunk_row_to_dict(r) for r in rows]

    def list_published_doc_ids_in_scope(self, scopes: list[str]) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, metadata_json, acl_tags_json FROM documents "
                "WHERE parse_status = 'published'"
            ).fetchall()
        result: list[str] = []
        for row in rows:
            if _is_visible(row["metadata_json"], row["acl_tags_json"], scopes):
                result.append(row["doc_id"])
        return result

    def search_fts(
        self, query: str, doc_ids: list[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        fts_query = _to_fts_query(query)
        placeholders = ",".join("?" for _ in doc_ids)
        sql = (
            f"SELECT c.chunk_id, c.doc_id, c.content, c.section_path, c.embedding_json, "
            f"c.embedding_model, d.title FROM chunks c "
            f"JOIN chunks_fts f ON f.chunk_id = c.chunk_id "
            f"JOIN documents d ON d.doc_id = c.doc_id "
            f"WHERE chunks_fts MATCH ? AND c.doc_id IN ({placeholders}) "
            f"ORDER BY rank LIMIT ?"
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, [fts_query, *doc_ids, limit]).fetchall()
        return [dict(r) for r in rows]

    def list_chunks_for_vector_recall(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT chunk_id, doc_id, content, section_path, embedding_json, embedding_model "
                f"FROM chunks WHERE doc_id IN ({placeholders}) AND embedding_json IS NOT NULL",
                doc_ids,
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["embedding"] = json.loads(r["embedding_json"]) if r["embedding_json"] else None
            out.append(d)
        return out

    def get_doc_title(self, doc_id: str) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT title FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return row["title"] if row else ""

    def write_qa_log(self, **fields: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO qa_logs
                   (qa_log_id, session_id, user_id, question, rewritten_query,
                    retrieved_chunks_json, answer, model_name, prompt_version,
                    confidence, latency_ms, trace_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, ?)""",
                (
                    fields["qa_log_id"],
                    fields["session_id"],
                    fields.get("user_id"),
                    fields["question"],
                    fields.get("rewritten_query"),
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
            conn.commit()

    def write_feedback(
        self, trace_id: str, feedback_type: str, comment: str | None
    ) -> str:
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feedbacks").fetchone()["c"]
            feedback_id = f"fb_{count + 1:03d}"
            conn.execute(
                """INSERT INTO feedbacks
                   (feedback_id, qa_log_id, trace_id, feedback_type, comment, user_id, created_at)
                   VALUES (?, NULL, ?, ?, ?, NULL, ?)""",
                (feedback_id, trace_id, feedback_type, comment, _now()),
            )
            conn.commit()
            return feedback_id


def _document_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "doc_id": row["doc_id"],
        "title": row["title"],
        "source_uri": row["source_uri"],
        "mime_type": row["mime_type"],
        "metadata": json.loads(row["metadata_json"]),
        "acl_tags": json.loads(row["acl_tags_json"]),
        "parse_status": row["parse_status"],
        "parse_error": row["parse_error"],
        "chunk_count": row["chunk_count"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _chunk_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "chunk_id": row["chunk_id"],
        "doc_id": row["doc_id"],
        "chunk_no": row["chunk_no"],
        "content": row["content"],
        "section_path": row["section_path"],
        "page_no": row["page_no"],
        "embedding": json.loads(row["embedding_json"]) if row["embedding_json"] else None,
        "embedding_model": row["embedding_model"],
    }


def _is_visible(metadata_json: str, acl_tags_json: str, scopes: list[str]) -> bool:
    if not scopes:
        return True
    visible = set(json.loads(acl_tags_json))
    for value in json.loads(metadata_json).values():
        visible.add(str(value))
    return bool(visible.intersection(scopes))


def _to_fts_query(query: str) -> str:
    """把自然语言转成 FTS5 OR 查询，任一 token 命中即召回。

    unicode61 把连续 CJK 作为单 token，ASCII 按单词切分；用 OR 提高召回。
    """
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return query
    return " OR ".join(f'"{t}"' for t in tokens)
