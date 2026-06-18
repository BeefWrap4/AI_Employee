"""baseline: capture existing store schemas

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-18

This migration captures the tables the platform's in-process stores
already create via ``CREATE TABLE IF NOT EXISTS``.  It is intentionally
written with ``IF NOT EXISTS`` so it is idempotent against databases
that were bootstrapped by the stores themselves (the pre-Alembic path).
From this revision forward, all schema changes go through Alembic.

Tables covered:
  * knowledge-api: documents, chunks, chunks_fts (+ triggers), qa_logs, feedbacks
  * rca-agent: rca_objects, candidate_knowledge
  * agent-platform-api: agent_runs, agent_run_events, eval_runs
  * tool-registry: tools
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- knowledge-api -----------------------------------------------------
    op.execute(
        """
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
        )
        """
    )
    op.execute(
        """
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
            created_at TEXT NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
        "chunk_id UNINDEXED, content, section_path, tokenize='unicode61')"
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(chunk_id, content, section_path)
            VALUES (new.chunk_id, new.content, new.section_path);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
        END
        """
    )
    op.execute(
        """
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
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedbacks (
            feedback_id TEXT PRIMARY KEY,
            qa_log_id TEXT,
            trace_id TEXT NOT NULL,
            feedback_type TEXT NOT NULL,
            comment TEXT,
            user_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    # --- rca-agent ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rca_objects (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (object_type, object_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_knowledge (
            candidate_id TEXT PRIMARY KEY,
            source_report_id TEXT NOT NULL,
            source_incident_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            root_cause_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            evidence_summary TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            reviewer TEXT,
            review_comment TEXT,
            imported_doc_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # --- agent-platform-api ------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            template_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            input_json TEXT NOT NULL,
            output_json TEXT,
            node_trace_json TEXT NOT NULL,
            tool_calls_json TEXT NOT NULL,
            approval_status TEXT NOT NULL,
            resume_from_node TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_run_events_run "
        "ON agent_run_events(run_id, event_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_runs (
            eval_run_id  TEXT PRIMARY KEY,
            eval_type    TEXT NOT NULL,
            template_id  TEXT NOT NULL,
            golden_path  TEXT NOT NULL,
            status       TEXT NOT NULL,
            report_json  TEXT,
            summary      TEXT,
            error        TEXT,
            trace_id     TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )

    # --- tool-registry -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tools (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            input_schema TEXT NOT NULL,
            output_schema TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            service_name TEXT,
            version TEXT NOT NULL DEFAULT 'v1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Drop all baseline tables.

    The FTS virtual table and its triggers are dropped first because
    they depend on ``chunks``.
    """
    op.execute("DROP TRIGGER IF EXISTS chunks_ad")
    op.execute("DROP TRIGGER IF EXISTS chunks_ai")
    op.execute("DROP TABLE IF EXISTS chunks_fts")
    for table in (
        "feedbacks",
        "qa_logs",
        "chunks",
        "documents",
        "candidate_knowledge",
        "rca_objects",
        "agent_run_events",
        "agent_runs",
        "eval_runs",
        "tools",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
