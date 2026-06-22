"""SQLite persistence for the approval-service.

Stores :class:`ApprovalTask` rows.  The schema is also created by the
Alembic migration ``0002_approval_tasks`` (dialect-aware, SQLite +
Postgres).  ``init_schema`` here is idempotent (``CREATE TABLE IF NOT
EXISTS``) so the service can bootstrap a fresh dev DB without Alembic,
and the two paths stay in sync.

JSON-valued fields (supplement_attachments, transfers) are serialised
to TEXT columns so the table is portable across SQLite and Postgres
without dialect-specific JSON types.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DEFAULT_DATA_DIR = "./var/data"
DB_FILENAME = "approval.sqlite3"

_LOG = logging.getLogger(__name__)
_WARNED_FALLBACK = False


def default_db_path() -> str:
    data_dir = os.environ.get("PLATFORM_DATA_DIR", DEFAULT_DATA_DIR)
    return os.path.join(data_dir, DB_FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT,
    comment TEXT,
    supplement_request TEXT,
    supplement_response TEXT,
    assignee TEXT,
    routed_to TEXT,
    deadline_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delegates_json TEXT NOT NULL DEFAULT '[]',
    delegated_by TEXT,
    supplement_note TEXT,
    supplement_attachments_json TEXT NOT NULL DEFAULT '[]',
    supplement_requested_by TEXT,
    supplement_resolved_by TEXT,
    transfers_json TEXT NOT NULL DEFAULT '[]',
    current_approver TEXT,
    escalated_at TEXT,
    escalated_to TEXT,
    escalation_reason TEXT
);
"""


# Columns stored as JSON text.  Each is decoded back to a Python object
# on read so callers see the same shape the agent-platform produces.
_JSON_COLUMNS = {
    "delegates": "delegates_json",
    "supplement_attachments": "supplement_attachments_json",
    "transfers": "transfers_json",
}


class ApprovalTaskStore:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        db: Any | None = None,
    ) -> None:
        # R28-PG: when ``db`` (a common-schemas ``DB`` wrapper) is supplied
        # the store talks to Postgres (or any backend ``open_db`` supports)
        # via the unified ``db.execute(sql, params)`` surface with ``?``
        # placeholders + dict rows.  Otherwise the legacy sqlite3 path is
        # used unchanged.
        self._db = db
        # An externally-supplied connection wins (used by tests for
        # isolation and by callers that already own a DB handle).
        self._external_conn = connection
        if db is not None:
            self.db_path = "<pg-backend>"
        elif connection is not None:
            self.db_path = "<external-connection>"
        else:
            self.db_path = db_path or default_db_path()
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> Any:
        if self._db is not None:
            return self._db
        if self._external_conn is not None:
            return self._external_conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _release(self, conn: Any) -> None:
        # The shared DB wrapper + external connections are not closed here.
        if self._db is not None or self._external_conn is not None:
            return
        conn.close()

    def init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(_SCHEMA)
                if self._db is None and self._external_conn is None:
                    conn.commit()
            finally:
                self._release(conn)

    def upsert(self, payload: dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT task_id FROM approval_tasks WHERE task_id = ?",
                    (payload["task_id"],),
                ).fetchone()
                now = payload.get("updated_at") or _now_iso()
                created = payload.get("created_at") or now
                delegates = json.dumps(payload.get("delegates", []), ensure_ascii=False)
                attachments = json.dumps(
                    payload.get("supplement_attachments", []), ensure_ascii=False
                )
                transfers = json.dumps(payload.get("transfers", []), ensure_ascii=False)
                if existing is None:
                    conn.execute(
                        """INSERT INTO approval_tasks
                           (task_id, run_id, template_id, requested_by, status,
                            risk_level, reason, decided_by, comment,
                            supplement_request, supplement_response, assignee,
                            routed_to, deadline_at, created_at, updated_at,
                            delegates_json, delegated_by, supplement_note,
                            supplement_attachments_json, supplement_requested_by,
                            supplement_resolved_by, transfers_json, current_approver,
                            escalated_at, escalated_to, escalation_reason)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            payload["task_id"],
                            payload["run_id"],
                            payload["template_id"],
                            payload["requested_by"],
                            payload["status"],
                            payload["risk_level"],
                            payload["reason"],
                            payload.get("decided_by"),
                            payload.get("comment"),
                            payload.get("supplement_request"),
                            payload.get("supplement_response"),
                            payload.get("assignee"),
                            payload.get("routed_to"),
                            payload.get("deadline_at"),
                            created,
                            now,
                            delegates,
                            payload.get("delegated_by"),
                            payload.get("supplement_note"),
                            attachments,
                            payload.get("supplement_requested_by"),
                            payload.get("supplement_resolved_by"),
                            transfers,
                            payload.get("current_approver"),
                            payload.get("escalated_at"),
                            payload.get("escalated_to"),
                            payload.get("escalation_reason"),
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE approval_tasks
                           SET run_id = ?, template_id = ?, requested_by = ?, status = ?,
                               risk_level = ?, reason = ?, decided_by = ?, comment = ?,
                               supplement_request = ?, supplement_response = ?, assignee = ?,
                               routed_to = ?, deadline_at = ?, updated_at = ?,
                               delegates_json = ?, delegated_by = ?, supplement_note = ?,
                               supplement_attachments_json = ?, supplement_requested_by = ?,
                               supplement_resolved_by = ?, transfers_json = ?, current_approver = ?,
                               escalated_at = ?, escalated_to = ?, escalation_reason = ?
                           WHERE task_id = ?""",
                        (
                            payload["run_id"],
                            payload["template_id"],
                            payload["requested_by"],
                            payload["status"],
                            payload["risk_level"],
                            payload["reason"],
                            payload.get("decided_by"),
                            payload.get("comment"),
                            payload.get("supplement_request"),
                            payload.get("supplement_response"),
                            payload.get("assignee"),
                            payload.get("routed_to"),
                            payload.get("deadline_at"),
                            now,
                            delegates,
                            payload.get("delegated_by"),
                            payload.get("supplement_note"),
                            attachments,
                            payload.get("supplement_requested_by"),
                            payload.get("supplement_resolved_by"),
                            transfers,
                            payload.get("current_approver"),
                            payload.get("escalated_at"),
                            payload.get("escalated_to"),
                            payload.get("escalation_reason"),
                            payload["task_id"],
                        ),
                    )
                if self._external_conn is None:
                    conn.commit()
            finally:
                self._release(conn)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM approval_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            finally:
                self._release(conn)
        return _row_to_dict(row) if row else None

    def list(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size
        with self._lock:
            conn = self._connect()
            try:
                if status:
                    total = conn.execute(
                        "SELECT COUNT(*) AS count FROM approval_tasks WHERE status = ?",
                        (status,),
                    ).fetchone()["count"]
                    rows = conn.execute(
                        "SELECT * FROM approval_tasks WHERE status = ? "
                        "ORDER BY created_at, task_id LIMIT ? OFFSET ?",
                        (status, page_size, offset),
                    ).fetchall()
                else:
                    total = conn.execute("SELECT COUNT(*) AS count FROM approval_tasks").fetchone()[
                        "count"
                    ]
                    rows = conn.execute(
                        "SELECT * FROM approval_tasks "
                        "ORDER BY created_at, task_id LIMIT ? OFFSET ?",
                        (page_size, offset),
                    ).fetchall()
            finally:
                self._release(conn)
        return [_row_to_dict(row) for row in rows], int(total)


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for py_name, db_name in _JSON_COLUMNS.items():
        raw = data.pop(db_name, None)
        if isinstance(raw, str) and raw:
            try:
                data[py_name] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data[py_name] = []
        else:
            data[py_name] = []
    return data


def build_approval_store(
    *,
    db_path: str | None = None,
    database_url: str | None = None,
) -> ApprovalTaskStore:
    """Pick ApprovalTaskStore backend based on DATABASE_URL.

    Defaults to the SQLite path (``ApprovalTaskStore`` with a local
    ``approval.sqlite3`` file) so dev/test behaviour is unchanged when
    ``DATABASE_URL`` is unset.  When set to a Postgres URL, opens a PG
    connection via the shared :func:`open_db` wrapper and the store
    uses the unified ``db.execute(sql, params)`` surface (``?``
    placeholders translated to ``%s`` automatically).

    The ``approval_tasks`` table is created idempotently by
    :meth:`ApprovalTaskStore.init_schema` (and by Alembic migration
    ``0002_approval_tasks``).

    R29-A: when ``DATABASE_URL`` is unset and the SQLite fallback is
    chosen, emit a one-shot deprecation warning so operators running
    the production chart can see they're on the legacy default.
    """
    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    if url:
        from ai_employee.common_schemas.db import detect_backend

        if detect_backend(url).name == "POSTGRES":
            from ai_employee.common_schemas.db import open_db

            db = open_db(url, row_factory="dict")
            return ApprovalTaskStore(db=db)
    global _WARNED_FALLBACK  # module-level throttle
    if not _WARNED_FALLBACK:
        _WARNED_FALLBACK = True
        _LOG.warning(
            "approval-service: DATABASE_URL is unset; falling back to local "
            "SQLite store. Set DATABASE_URL=postgresql://... for production.",
        )
    return ApprovalTaskStore(db_path=db_path or default_db_path())


__all__ = [
    "DB_FILENAME",
    "DEFAULT_DATA_DIR",
    "ApprovalTaskStore",
    "build_approval_store",
    "default_db_path",
]
