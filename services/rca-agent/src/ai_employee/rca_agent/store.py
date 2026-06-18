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
    CandidateKnowledge,
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
        self.candidates.clear()
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
        self._load_candidates()
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

    def save_candidate(self, candidate: CandidateKnowledge) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO candidate_knowledge (
                       candidate_id, source_report_id, source_incident_id,
                       hypothesis_id, root_cause_type, title, content,
                       evidence_summary, review_status, reviewer, review_comment,
                       imported_doc_id, created_at, reviewed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                     source_report_id = excluded.source_report_id,
                     source_incident_id = excluded.source_incident_id,
                     hypothesis_id = excluded.hypothesis_id,
                     root_cause_type = excluded.root_cause_type,
                     title = excluded.title,
                     content = excluded.content,
                     evidence_summary = excluded.evidence_summary,
                     review_status = excluded.review_status,
                     reviewer = excluded.reviewer,
                     review_comment = excluded.review_comment,
                     imported_doc_id = excluded.imported_doc_id,
                     created_at = excluded.created_at,
                     reviewed_at = excluded.reviewed_at""",
                (
                    candidate.candidate_id,
                    candidate.source_report_id,
                    candidate.source_incident_id,
                    candidate.hypothesis_id,
                    candidate.root_cause_type,
                    candidate.title,
                    candidate.content,
                    candidate.evidence_summary,
                    candidate.review_status,
                    candidate.reviewer,
                    candidate.review_comment,
                    candidate.imported_doc_id,
                    candidate.created_at,
                    candidate.reviewed_at,
                ),
            )
            conn.commit()

    def _load_candidates(self) -> None:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT candidate_id, source_report_id, source_incident_id,
                          hypothesis_id, root_cause_type, title, content,
                          evidence_summary, review_status, reviewer, review_comment,
                          imported_doc_id, created_at, reviewed_at
                   FROM candidate_knowledge"""
            ).fetchall()
        for row in rows:
            candidate = CandidateKnowledge(
                candidate_id=row["candidate_id"],
                source_report_id=row["source_report_id"],
                source_incident_id=row["source_incident_id"],
                hypothesis_id=row["hypothesis_id"],
                root_cause_type=row["root_cause_type"],
                title=row["title"],
                content=row["content"],
                evidence_summary=row["evidence_summary"],
                review_status=row["review_status"],
                reviewer=row["reviewer"],
                review_comment=row["review_comment"],
                imported_doc_id=row["imported_doc_id"],
                created_at=row["created_at"],
                reviewed_at=row["reviewed_at"],
            )
            self.candidates[candidate.candidate_id] = candidate

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
        self.candidate_count = _max_suffix(self.candidates)


def _max_suffix(items: dict[str, Any]) -> int:
    max_value = 0
    for key in items:
        try:
            max_value = max(max_value, int(key.rsplit("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max_value
