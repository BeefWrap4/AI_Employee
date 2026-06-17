from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from ai_employee.rca_agent.runtime import RcaStore
from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    IncidentResponse,
    RcaReportResponse,
    RcaRunResponse,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rca_objects (
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (object_type, object_id)
);
"""


class SQLiteRcaStore(RcaStore):
    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()
        self.load()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def load(self) -> None:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT object_type, object_id, payload_json FROM rca_objects"
            ).fetchall()
        self.alarms.clear()
        self.incidents.clear()
        self.runs.clear()
        self.reports.clear()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if row["object_type"] == "alarm":
                self.alarms[row["object_id"]] = AlarmEvent(**payload)
            elif row["object_type"] == "incident":
                self.incidents[row["object_id"]] = IncidentResponse(**payload)
            elif row["object_type"] == "run":
                self.runs[row["object_id"]] = RcaRunResponse(**payload)
            elif row["object_type"] == "report":
                self.reports[row["object_id"]] = RcaReportResponse(**payload)
        self._sync_counters()

    def save_alarm(self, alarm: AlarmEvent) -> None:
        self._upsert("alarm", alarm.alarm_event_id, alarm.model_dump(mode="json"))

    def save_incident(self, incident: IncidentResponse) -> None:
        self._upsert("incident", incident.incident_id, incident.model_dump(mode="json"))

    def save_run(self, run: RcaRunResponse) -> None:
        self._upsert("run", run.run_id, run.model_dump(mode="json"))

    def save_report(self, report: RcaReportResponse) -> None:
        self._upsert("report", report.report_id, report.model_dump(mode="json"))

    def save_run_and_report(self, run: RcaRunResponse, report: RcaReportResponse) -> None:
        self.save_run(run)
        self.save_report(report)

    def _upsert(self, object_type: str, object_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO rca_objects (object_type, object_id, payload_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(object_type, object_id) DO UPDATE SET
                     payload_json = excluded.payload_json,
                     updated_at = excluded.updated_at""",
                (
                    object_type,
                    object_id,
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def _sync_counters(self) -> None:
        self.alarm_count = _max_suffix(self.alarms)
        self.incident_count = _max_suffix(self.incidents)
        self.run_count = _max_suffix(self.runs)
        self.report_count = _max_suffix(self.reports)


def _max_suffix(items: dict[str, Any]) -> int:
    max_value = 0
    for key in items:
        try:
            max_value = max(max_value, int(key.rsplit("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max_value
