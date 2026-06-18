"""tool_call_log persistence (spec §6.4).

Stores every tool invocation: run_id, tool_name, input, output_summary,
status (success/failed/timeout), latency_ms, error_code.  Drives the
``tool_call_success_rate`` metric and audit queries.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_call_log (
    log_id TEXT PRIMARY KEY,
    run_id TEXT,
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_summary TEXT,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_call_run ON tool_call_log(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_call_tool ON tool_call_log(tool_name);
"""


class ToolCallLogStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.path.join(
            os.environ.get("PLATFORM_DATA_DIR", "./var/data"), "tool_call_log.sqlite3"
        )
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._counter = 0
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
        input: dict[str, Any] | None,
        output_summary: str | None,
        status: str,
        latency_ms: int,
        error_code: str | None,
    ) -> str:
        with self._lock, self._connect() as conn:
            self._counter += 1
            log_id = f"tcl_{self._counter:06d}"
            conn.execute(
                """INSERT INTO tool_call_log
                   (log_id, run_id, tool_name, input_json, output_summary,
                    status, latency_ms, error_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log_id,
                    run_id,
                    tool_name,
                    json.dumps(input or {}, ensure_ascii=False),
                    output_summary,
                    status,
                    latency_ms,
                    error_code,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        return log_id

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_call_log WHERE run_id = ? ORDER BY log_id",
                (run_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list(
        self, *, tool_name: str | None = None, page: int = 1, page_size: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        where = "WHERE tool_name = ?" if tool_name else ""
        params: list[Any] = [tool_name] if tool_name else []
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size
        with self._lock, self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM tool_call_log {where}", params
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = conn.execute(
                f"""SELECT * FROM tool_call_log {where}
                    ORDER BY log_id DESC LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ).fetchall()
        return [_row_to_dict(r) for r in rows], total

    def success_rate(self, *, tool_name: str | None = None) -> float:
        where = "WHERE tool_name = ?" if tool_name else ""
        params: list[Any] = [tool_name] if tool_name else []
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""SELECT
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok,
                       COUNT(*) AS total
                   FROM tool_call_log {where}""",
                params,
            ).fetchone()
        total = int(row["total"]) if row and row["total"] else 0
        if total == 0:
            return 1.0
        return int(row["ok"] or 0) / total


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    raw = data.get("input_json")
    if isinstance(raw, str) and raw:
        try:
            data["input"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data["input"] = raw
    return data


__all__ = ["ToolCallLogStore"]
