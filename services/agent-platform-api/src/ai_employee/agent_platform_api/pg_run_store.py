"""Postgres-backed agent-run store (R16-4).

Persists agent runs + run events through the shared :class:`DB`
abstraction.  Same schema as :class:`AgentRunStore` (SQLite), with the
``agent_run_events.event_id`` column adapted per dialect
(AUTOINCREMENT on SQLite, BIGINT IDENTITY on Postgres).  Selected by
:func:`build_run_store` when ``DATABASE_URL`` points at Postgres.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ai_employee.common_schemas.db import DB, Backend, open_db

if TYPE_CHECKING:
    from ai_employee.agent_platform_api.run_store import AgentRunStore

_LOG = logging.getLogger(__name__)
_WARNED_FALLBACK = False

_SCHEMA_BASE = """
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
);
"""

_SCHEMA_EVENTS_SQLITE = """
CREATE TABLE IF NOT EXISTS agent_run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);
"""

_SCHEMA_EVENTS_PG = """
CREATE TABLE IF NOT EXISTS agent_run_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);
"""

_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_agent_run_events_run ON agent_run_events(run_id, event_id);"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PgAgentRunStore:
    """Postgres-backed agent run persistence (dual-backend with AgentRunStore)."""

    def __init__(self, *, db: DB) -> None:
        self._db = db
        self.backend = db.backend
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        with self._lock:
            self._db.execute(_SCHEMA_BASE)
            if self.backend == Backend.POSTGRES:
                self._db.execute(_SCHEMA_EVENTS_PG)
            else:
                self._db.execute(_SCHEMA_EVENTS_SQLITE)
            self._db.execute(_INDEX)
            self._db.commit()

    # -- writes ---------------------------------------------------------------
    def upsert_run(self, payload: dict[str, Any]) -> str:
        """Persist or update a run.

        R30-A: if ``payload`` omits ``run_id`` (caller wants the store to
        mint one), generate a uuid4-suffixed id (``run_<hex8>``) so
        concurrent writers on the same PG backend never collide on the
        PK.  Pre-R30 the platform minted ids from an in-memory counter
        (``runtime.create_run``), which races under multi-replica PG
        deployments.  Returns the persisted ``run_id``.
        """
        run_id = payload.get("run_id")
        if not run_id:
            run_id = f"run_{uuid.uuid4().hex[:8]}"
            payload = {**payload, "run_id": run_id}
        with self._lock:
            existing = self._db.execute(
                "SELECT run_id FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                self._db.execute(
                    """INSERT INTO agent_runs
                       (run_id, template_id, agent_name, status, trace_id,
                        requested_by, input_json, output_json, node_trace_json,
                        tool_calls_json, approval_status, resume_from_node,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        payload["template_id"],
                        payload["agent_name"],
                        payload["status"],
                        payload["trace_id"],
                        payload["requested_by"],
                        json.dumps(payload.get("input", {}), ensure_ascii=False),
                        json.dumps(payload.get("output", {}), ensure_ascii=False),
                        json.dumps(payload.get("node_trace", []), ensure_ascii=False),
                        json.dumps(payload.get("tool_calls", []), ensure_ascii=False),
                        payload.get("approval_status", "not_required"),
                        payload.get("resume_from_node"),
                        payload.get("created_at") or _now_iso(),
                        _now_iso(),
                    ),
                )
            else:
                self._db.execute(
                    """UPDATE agent_runs
                       SET status = ?, output_json = ?, node_trace_json = ?,
                           tool_calls_json = ?, approval_status = ?,
                           resume_from_node = ?, updated_at = ?
                       WHERE run_id = ?""",
                    (
                        payload["status"],
                        json.dumps(payload.get("output", {}), ensure_ascii=False),
                        json.dumps(payload.get("node_trace", []), ensure_ascii=False),
                        json.dumps(payload.get("tool_calls", []), ensure_ascii=False),
                        payload.get("approval_status", "not_required"),
                        payload.get("resume_from_node"),
                        _now_iso(),
                        run_id,
                    ),
                )
            for event in payload.get("new_events", []) or []:
                self._db.execute(
                    """INSERT INTO agent_run_events
                       (run_id, node_name, status, detail, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        event["node_name"],
                        event["status"],
                        event.get("detail"),
                        event.get("created_at") or _now_iso(),
                    ),
                )
            self._db.commit()
            return run_id

    # -- reads ----------------------------------------------------------------
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        events = self._db.execute(
            """SELECT node_name, status, detail, created_at
               FROM agent_run_events WHERE run_id = ?
               ORDER BY event_id ASC""",
            (run_id,),
        ).fetchall()
        data = _row_to_dict(row)
        data["events"] = [dict(e) for e in events]
        return data

    def list_runs(
        self,
        *,
        template_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if template_id is not None:
            clauses.append("template_id = ?")
            params.append(template_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        count_row = self._db.execute(
            f"SELECT COUNT(*) AS c FROM agent_runs {where}",
            params,
        ).fetchone()
        total = int(count_row["c"] if count_row else 0)
        offset = max(0, (page - 1) * page_size)
        rows = self._db.execute(
            f"""SELECT * FROM agent_runs {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?""",
            [*params, page_size, offset],
        ).fetchall()
        return [_row_to_dict(r) for r in rows], total


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "template_id": row["template_id"],
        "agent_name": row["agent_name"],
        "status": row["status"],
        "trace_id": row["trace_id"],
        "requested_by": row["requested_by"],
        "input": json.loads(row["input_json"]) if row.get("input_json") else {},
        "output": json.loads(row["output_json"]) if row.get("output_json") else {},
        "node_trace": json.loads(row["node_trace_json"]) if row.get("node_trace_json") else [],
        "tool_calls": json.loads(row["tool_calls_json"]) if row.get("tool_calls_json") else [],
        "approval_status": row["approval_status"],
        "resume_from_node": row.get("resume_from_node"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def build_run_store(
    *,
    db_path: str | None = None,
    database_url: str | None = None,
) -> AgentRunStore | PgAgentRunStore:
    """Pick AgentRunStore (default, SQLite) or PgAgentRunStore (Postgres).

    R29-A: when ``DATABASE_URL`` is unset and the SQLite fallback is
    chosen, emit a one-shot deprecation warning so operators running
    the production chart can see they're on the legacy default.
    """
    from ai_employee.common_schemas.db import detect_backend

    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    backend = detect_backend(url)
    if backend == Backend.POSTGRES:
        db = open_db(url, row_factory="dict")
        store = PgAgentRunStore(db=db)
        store.init_schema()
        return store
    global _WARNED_FALLBACK  # module-level throttle
    if not _WARNED_FALLBACK:
        _WARNED_FALLBACK = True
        _LOG.warning(
            "agent-platform-api: DATABASE_URL is unset; falling back to local "
            "SQLite store. Set DATABASE_URL=postgresql://... for production.",
        )
    from ai_employee.agent_platform_api.run_store import AgentRunStore

    return AgentRunStore(db_path=db_path)


__all__ = ["PgAgentRunStore", "build_run_store"]
