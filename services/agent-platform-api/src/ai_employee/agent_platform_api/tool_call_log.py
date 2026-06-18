"""Platform tool_call_log (spec §5.3 + §6.4).

SQLite-backed record of every tool invocation.  Drives:

* ``tool_call_success_rate`` — :meth:`success_rate`
* ``tool_latency_p95`` — :meth:`latency_p95`
* failure breakdown by ``error_code`` — :meth:`failure_breakdown`

Each row has run_id (nullable for dry-runs), tool_name, input/output
JSON-ish strings, status, latency_ms, error_code, created_at.

A Pydantic-style dataclass :class:`ToolCallRecord` is exposed so
callers don't need to touch the raw dicts.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_tool_call_log (
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
CREATE INDEX IF NOT EXISTS idx_ptcl_run
    ON platform_tool_call_log(run_id);
CREATE INDEX IF NOT EXISTS idx_ptcl_tool
    ON platform_tool_call_log(tool_name);
"""


@dataclass
class ToolCallRecord:
    log_id: int
    run_id: str | None
    tool_name: str
    input_summary: str
    output_summary: str | None
    status: str
    latency_ms: int | None
    error_code: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.log_id,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "created_at": self.created_at,
        }


class PlatformToolCallLogStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.path.join(
            os.environ.get("PLATFORM_DATA_DIR", "./var/data"),
            "platform_tool_call_log.sqlite3",
        )
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
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
                """INSERT INTO platform_tool_call_log
                   (run_id, tool_name, input_summary, output_summary,
                    status, latency_ms, error_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    tool_name,
                    input_summary[:1000],
                    output_summary,
                    status,
                    latency_ms,
                    error_code,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def list_for_run(self, run_id: str | None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if run_id is None:
                rows = conn.execute(
                    "SELECT * FROM platform_tool_call_log WHERE run_id IS NULL ORDER BY log_id",
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM platform_tool_call_log WHERE run_id = ? ORDER BY log_id",
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
                   FROM platform_tool_call_log {where}""",
                params,
            ).fetchone()
        total = int(row["total"]) if row and row["total"] else 0
        if total == 0:
            return 1.0
        return int(row["ok"] or 0) / total

    def latency_p95(self, *, tool_name: str | None = None) -> float:
        where = "WHERE tool_name = ?" if tool_name else ""
        params: list[Any] = [tool_name] if tool_name else []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT latency_ms FROM platform_tool_call_log
                    {where} ORDER BY latency_ms""",
                params,
            ).fetchall()
        if not rows:
            return 0.0
        idx = max(0, int(0.95 * (len(rows) - 1)))
        return float(rows[idx]["latency_ms"])

    def failure_breakdown(
        self, *, tool_name: str | None = None,
    ) -> dict[str, int]:
        where = "WHERE status='failure'"
        params: list[Any] = []
        if tool_name:
            where += " AND tool_name = ?"
            params.append(tool_name)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT error_code, COUNT(*) AS n
                    FROM platform_tool_call_log {where}
                    GROUP BY error_code""",
                params,
            ).fetchall()
        return {r["error_code"] or "unknown": int(r["n"]) for r in rows}


__all__ = ["PlatformToolCallLogStore", "ToolCallRecord"]
