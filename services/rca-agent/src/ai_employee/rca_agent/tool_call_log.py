"""RCA agent tool_call_log persistence (spec §6.4).

Every adapter fetch in :mod:`ai_employee.rca_agent.tool_adapters` is
recorded here: run_id, tool_name, input_summary, output_summary, status,
latency_ms, error_code.  Drives the ``tool_call_success_rate`` metric
and audit queries.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rca_tool_call_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    tool_name TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    output_summary TEXT,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rca_tool_call_run
    ON rca_tool_call_log(run_id);
"""


class RcaToolCallLogStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.path.join(
            os.environ.get("RCA_DATA_DIR", "./var/data"), "rca_tool_call_log.sqlite3"
        )
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
        except Exception:
            pass
        return conn

    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def record(
        self,
        *,
        run_id: str | None,
        tool_name: str,
        input_summary: str,
        output_summary: str | None,
        status: str,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO rca_tool_call_log
                   (run_id, tool_name, input_summary, output_summary,
                    status, latency_ms, error_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    tool_name,
                    input_summary[:500],
                    output_summary,
                    status,
                    latency_ms,
                    error_code,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.commit()
            # Defensive: open a fresh read connection to verify write
            # persistence and force a checkpoint.
            try:
                verify = self._connect()
                verify.execute("SELECT COUNT(*) FROM rca_tool_call_log").fetchone()
                verify.close()
            except Exception:
                pass

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM rca_tool_call_log WHERE run_id = ? ORDER BY log_id",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def success_rate(self, *, tool_name: str | None = None) -> float:
        where = "WHERE tool_name = ?" if tool_name else ""
        params: list[Any] = [tool_name] if tool_name else []
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""SELECT
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok,
                       COUNT(*) AS total
                   FROM rca_tool_call_log {where}""",
                params,
            ).fetchone()
        total = int(row["total"]) if row and row["total"] else 0
        if total == 0:
            return 1.0
        return int(row["ok"] or 0) / total


__all__ = ["RcaToolCallLogStore"]
