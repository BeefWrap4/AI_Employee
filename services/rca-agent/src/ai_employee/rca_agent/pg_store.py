"""Postgres-backed RCA store (R16-4).

Persists RCA objects (alarms/incidents/runs/reports) through the shared
:class:`DB` abstraction into the ``rca_objects`` JSON table — the same
schema :class:`SQLiteRcaStore` uses, so the migration's baseline DDL
covers both.  Selected by :func:`build_rca_store` when ``DATABASE_URL``
points at Postgres.

The ``rca_objects`` table is a generic (object_type, object_id) → JSON
key-value store with ``ON CONFLICT ... DO UPDATE`` upserts, which is
portable across SQLite and Postgres unchanged.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ai_employee.common_schemas.db import DB, Backend, open_db
from ai_employee.rca_agent.runtime import RcaStore
from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    IncidentResponse,
    RcaReportResponse,
    RcaRunResponse,
)

if TYPE_CHECKING:
    from ai_employee.rca_agent.store import SQLiteRcaStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rca_objects (
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (object_type, object_id)
);

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
    reviewed_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PgRcaStore(RcaStore):
    """Postgres-backed RCA store (dual-backend with SQLiteRcaStore).

    Stores the in-memory aggregates (alarms/incidents/runs/reports) on the
    base :class:`RcaStore` and persists each via the ``rca_objects`` JSON
    table.  ``load_*`` methods rehydrate from the table.
    """

    def __init__(self, *, db: DB) -> None:
        super().__init__()
        self._db = db
        self.backend = db.backend
        self._lock = threading.Lock()

    # -- schema ---------------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            for stmt in _SCHEMA.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._db.execute(stmt)
            self._db.commit()

    # -- upsert / load helpers ------------------------------------------------
    def _upsert(self, object_type: str, object_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO rca_objects (object_type, object_id, payload_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(object_type, object_id) DO UPDATE SET
                     payload_json = excluded.payload_json,
                     updated_at = excluded.updated_at""",
                (object_type, object_id, json.dumps(payload, ensure_ascii=False), _now()),
            )
            self._db.commit()

    def _load_by_type(self, object_type: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT payload_json FROM rca_objects WHERE object_type = ?",
            (object_type,),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    # -- persistence (RcaStore overrides) ------------------------------------
    def save_alarm(self, alarm: AlarmEvent) -> None:
        self._upsert("alarm", alarm.alarm_event_id, alarm.model_dump(mode="json"))

    def save_incident(self, incident: IncidentResponse) -> None:
        self._upsert("incident", incident.incident_id, incident.model_dump(mode="json"))

    def save_run(self, run: RcaRunResponse) -> None:
        self._upsert("run", run.run_id, run.model_dump(mode="json"))

    def save_report(self, report: RcaReportResponse) -> None:
        self._upsert("report", report.report_id, report.model_dump(mode="json"))

    # -- loaders (return fresh dicts; caller constructs model objects) --------
    def load_alarms(self) -> dict[str, AlarmEvent]:
        out: dict[str, AlarmEvent] = {}
        for payload in self._load_by_type("alarm"):
            ev = AlarmEvent(**payload)
            out[ev.alarm_event_id] = ev
        return out

    def load_incidents(self) -> dict[str, IncidentResponse]:
        out: dict[str, IncidentResponse] = {}
        for payload in self._load_by_type("incident"):
            inc = IncidentResponse(**payload)
            out[inc.incident_id] = inc
        return out

    def load_runs(self) -> dict[str, RcaRunResponse]:
        out: dict[str, RcaRunResponse] = {}
        for payload in self._load_by_type("run"):
            run = RcaRunResponse(**payload)
            out[run.run_id] = run
        return out

    def load_reports(self) -> dict[str, RcaReportResponse]:
        out: dict[str, RcaReportResponse] = {}
        for payload in self._load_by_type("report"):
            rep = RcaReportResponse(**payload)
            out[rep.report_id] = rep
        return out


def build_rca_store(
    *,
    db_path: str | None = None,
    database_url: str | None = None,
) -> SQLiteRcaStore | PgRcaStore:
    """Pick SQLiteRcaStore (default) or PgRcaStore based on DATABASE_URL."""
    from ai_employee.common_schemas.db import detect_backend

    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    backend = detect_backend(url)
    if backend == Backend.POSTGRES:
        db = open_db(url, row_factory="dict")
        store = PgRcaStore(db=db)
        store.init_schema()
        return store
    from ai_employee.rca_agent.store import SQLiteRcaStore

    return SQLiteRcaStore(db_path=db_path or "./var/data/rca.sqlite3")


__all__ = ["PgRcaStore", "build_rca_store"]
