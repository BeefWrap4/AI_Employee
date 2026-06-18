"""SQLite persistence for agent runs (spec §3.2).

Stores agent run state, node trace, tool calls, approval tasks, and resume
tokens. Independent from the in-memory ``AgentPlatformStore`` so existing
runtime behaviour is preserved. Used by the ``/api/v1/agent-runs/{id}/resume``
endpoint to continue a paused run from its last completed node.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DEFAULT_DATA_DIR = "./var/data"
DB_FILENAME = "platform_runs.sqlite3"


def default_db_path() -> str:
    data_dir = os.environ.get("PLATFORM_DATA_DIR", DEFAULT_DATA_DIR)
    return os.path.join(data_dir, DB_FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS agent_run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_run_events_run
    ON agent_run_events(run_id, event_id);
"""


class AgentRunStore:
    """SQLite-backed agent run persistence."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # -- write helpers -----------------------------------------------------

    def upsert_run(self, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT run_id FROM agent_runs WHERE run_id = ?",
                (payload["run_id"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO agent_runs
                       (run_id, template_id, agent_name, status, trace_id,
                        requested_by, input_json, output_json, node_trace_json,
                        tool_calls_json, approval_status, resume_from_node,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["run_id"],
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
                conn.execute(
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
                        payload["run_id"],
                    ),
                )
            for event in payload.get("new_events", []) or []:
                conn.execute(
                    """INSERT INTO agent_run_events
                       (run_id, node_name, status, detail, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        payload["run_id"],
                        event["node_name"],
                        event["status"],
                        event.get("detail"),
                        event.get("created_at") or _now_iso(),
                    ),
                )
            conn.commit()

    def mark_resumed(self, run_id: str, resume_from_node: str | None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE agent_runs
                   SET status = ?, resume_from_node = ?, updated_at = ?
                   WHERE run_id = ?""",
                ("running", resume_from_node, _now_iso(), run_id),
            )
            conn.commit()

    # -- read helpers ------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            events = conn.execute(
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size

        with self._lock, self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM agent_runs{where}", params
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = conn.execute(
                f"""SELECT * FROM agent_runs{where}
                    ORDER BY run_id ASC
                    LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ).fetchall()
        return [_row_to_dict(row) for row in rows], total


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("input_json", "output_json", "node_trace_json", "tool_calls_json"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            try:
                decoded = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                decoded = raw
            data[_json_alias(key)] = decoded
    return data


def _json_alias(key: str) -> str:
    return {
        "input_json": "input",
        "output_json": "output",
        "node_trace_json": "node_trace",
        "tool_calls_json": "tool_calls",
    }[key]


__all__ = ["DB_FILENAME", "DEFAULT_DATA_DIR", "AgentRunStore", "default_db_path"]
