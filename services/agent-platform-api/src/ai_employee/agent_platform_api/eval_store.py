"""SQLite persistence for the platform eval center (spec §7.1).

Independent file ``${RCA_DATA_DIR}/platform_eval.sqlite3`` (default
``./var/data``). Uses ``sqlite3`` + ``threading.Lock`` in the same style as the
rca-agent store. Deliberately independent from the in-memory
``AgentPlatformStore`` runtime so existing platform run logic is untouched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
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
);
"""

DEFAULT_DATA_DIR = "./var/data"
DB_FILENAME = "platform_eval.sqlite3"


def default_db_path() -> str:
    data_dir = os.environ.get("RCA_DATA_DIR", DEFAULT_DATA_DIR)
    return os.path.join(data_dir, DB_FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvalStore:
    """SQLite-backed store for ``eval_runs`` records."""

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

    def _next_eval_run_id(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT eval_run_id FROM eval_runs ORDER BY eval_run_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "eval_001"
        last = row["eval_run_id"]
        try:
            n = int(last.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            n = 0
        return f"eval_{n + 1:03d}"

    def create_eval_run(
        self,
        *,
        eval_type: str,
        template_id: str,
        golden_path: str,
        trace_id: str | None = None,
        status: str = "running",
    ) -> str:
        """Insert a new eval_run row and return its ``eval_run_id``."""
        with self._lock, self._connect() as conn:
            eval_run_id = self._next_eval_run_id(conn)
            trace = trace_id or f"trace_{eval_run_id}"
            conn.execute(
                """INSERT INTO eval_runs
                   (eval_run_id, eval_type, template_id, golden_path, status,
                    report_json, summary, error, trace_id, created_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL)""",
                (
                    eval_run_id,
                    eval_type,
                    template_id,
                    golden_path,
                    status,
                    trace,
                    _now_iso(),
                ),
            )
            conn.commit()
        return eval_run_id

    def complete_eval_run(
        self,
        eval_run_id: str,
        *,
        report: dict[str, Any],
        summary: dict[str, Any],
        status: str = "completed",
    ) -> None:
        """Persist the final unified report + summary for a finished run."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE eval_runs
                   SET status = ?, report_json = ?, summary = ?, completed_at = ?
                   WHERE eval_run_id = ?""",
                (
                    status,
                    json.dumps(report, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    _now_iso(),
                    eval_run_id,
                ),
            )
            conn.commit()

    def fail_eval_run(self, eval_run_id: str, *, error: str, status: str = "failed") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE eval_runs
                   SET status = ?, error = ?, completed_at = ?
                   WHERE eval_run_id = ?""",
                (status, error, _now_iso(), eval_run_id),
            )
            conn.commit()

    # -- read helpers ------------------------------------------------------

    def get_eval_run(self, eval_run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM eval_runs WHERE eval_run_id = ?",
                (eval_run_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_eval_runs(
        self,
        *,
        eval_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if eval_type is not None:
            clauses.append("eval_type = ?")
            params.append(eval_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size

        with self._lock, self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM eval_runs{where}", params
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = conn.execute(
                f"""SELECT * FROM eval_runs{where}
                    ORDER BY eval_run_id ASC
                    LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ).fetchall()
        return [_row_to_dict(row) for row in rows], total


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    # Decode JSON columns for API convenience; keep raw if decode fails.
    for key in ("report_json", "summary"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            try:
                data[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return data


__all__ = ["EvalStore", "default_db_path", "DB_FILENAME", "DEFAULT_DATA_DIR"]
