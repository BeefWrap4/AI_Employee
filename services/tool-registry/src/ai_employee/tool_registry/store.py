"""SQLite persistence for the tool-registry service.

Stores registered :class:`ToolSpec` rows.  Handlers are kept in-memory
(the registry reloads specs from disk on startup and re-binds handlers
for the built-in demo tools); this keeps the schema focused on the
declarative contract (name, description, schemas, risk level, service).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DEFAULT_DATA_DIR = "./var/data"
DB_FILENAME = "tool_registry.sqlite3"


def default_db_path() -> str:
    data_dir = os.environ.get("PLATFORM_DATA_DIR", DEFAULT_DATA_DIR)
    return os.path.join(data_dir, DB_FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
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
);
"""


class ToolRegistryStore:
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
            # Idempotent column migrations for governance fields added later.
            for col, decl in (
                ("timeout_ms", "INTEGER NOT NULL DEFAULT 5000"),
                ("retry_policy", "TEXT NOT NULL DEFAULT '{}'"),
                ("health_check_url", "TEXT"),
                # R25-T: live health probe writes back here.  ``unknown``
                # is the default until a probe runs.
                ("health_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("health_latency_ms", "REAL NOT NULL DEFAULT 0"),
                ("health_error", "TEXT"),
                ("health_checked_at", "TEXT"),
            ):
                cols = {r[1] for r in conn.execute("PRAGMA table_info(tools)").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE tools ADD COLUMN {col} {decl}")
            conn.commit()

    def upsert(self, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT name FROM tools WHERE name = ?", (payload["name"],)
            ).fetchone()
            now = _now_iso()
            if existing is None:
                conn.execute(
                    """INSERT INTO tools
                       (name, description, input_schema, output_schema,
                        risk_level, service_name, version, timeout_ms,
                        retry_policy, health_check_url, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["name"],
                        payload["description"],
                        json.dumps(payload["input_schema"], ensure_ascii=False),
                        json.dumps(payload["output_schema"], ensure_ascii=False),
                        payload["risk_level"],
                        payload.get("service_name"),
                        payload.get("version", "v1"),
                        int(payload.get("timeout_ms", 5000)),
                        json.dumps(
                            payload.get("retry_policy", {"max_retries": 0}), ensure_ascii=False
                        ),
                        payload.get("health_check_url"),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE tools
                       SET description = ?, input_schema = ?, output_schema = ?,
                           risk_level = ?, service_name = ?, version = ?,
                           timeout_ms = ?, retry_policy = ?, health_check_url = ?,
                           updated_at = ?
                       WHERE name = ?""",
                    (
                        payload["description"],
                        json.dumps(payload["input_schema"], ensure_ascii=False),
                        json.dumps(payload["output_schema"], ensure_ascii=False),
                        payload["risk_level"],
                        payload.get("service_name"),
                        payload.get("version", "v1"),
                        int(payload.get("timeout_ms", 5000)),
                        json.dumps(
                            payload.get("retry_policy", {"max_retries": 0}), ensure_ascii=False
                        ),
                        payload.get("health_check_url"),
                        now,
                        payload["name"],
                    ),
                )
            conn.commit()

    def delete(self, name: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM tools WHERE name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0

    def get(self, name: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tools WHERE name = ?", (name,)).fetchone()
        return _row_to_dict(row) if row else None

    def list(self, *, service_name: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if service_name:
                rows = conn.execute(
                    "SELECT * FROM tools WHERE service_name = ? ORDER BY name",
                    (service_name,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tools ORDER BY name").fetchall()
        return [_row_to_dict(row) for row in rows]

    def update_health_status(
        self,
        name: str,
        status: str,
        latency_ms: float = 0.0,
        error: str | None = None,
    ) -> bool:
        """Persist the latest probe result for ``name``.

        Returns ``True`` when the row was updated, ``False`` if the tool
        no longer exists (e.g. unregistered between probe iterations).
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """UPDATE tools
                   SET health_status = ?, health_latency_ms = ?,
                       health_error = ?, health_checked_at = ?,
                       updated_at = ?
                   WHERE name = ?""",
                (
                    status,
                    float(latency_ms),
                    error,
                    _now_iso(),
                    _now_iso(),
                    name,
                ),
            )
            conn.commit()
            return cur.rowcount > 0


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("input_schema", "output_schema", "retry_policy"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            try:
                data[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return data


__all__ = ["DB_FILENAME", "DEFAULT_DATA_DIR", "ToolRegistryStore", "default_db_path"]
