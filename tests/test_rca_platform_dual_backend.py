"""rca-agent + agent-platform dual-backend store tests (R16-4).

The RCA object store (``rca_objects`` JSON table) and the platform
agent-run store both persist through the shared :class:`DB` abstraction,
so the same SQL runs on SQLite and Postgres.  We prove portability by
running the persistence contract against a SQLite DB (always) and a
live Postgres (when ``TEST_POSTGRES_URL`` is set).
"""

from __future__ import annotations

import os

import pytest
from ai_employee.common_schemas.db import Backend, open_db
from ai_employee.rca_agent.pg_store import PgRcaStore, build_rca_store
from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    IncidentResponse,
    RawAlarmEvent,
    RcaReportResponse,
    RcaRunResponse,
)
from ai_employee.rca_agent.store import SQLiteRcaStore

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _truncate_rca_objects(db) -> None:
    """Clear the shared live-PG ``rca_objects`` table so each PG-leg test
    starts from a clean slate, mirroring the fresh ``tmp_path`` SQLite DB
    the SQLite leg gets.  SQLite legs are no-ops (each test already uses a
    brand-new tmp file).  Only the live PG connection is shared across the
    whole file, so without this the PG leg accumulates rows from earlier
    tests and ``load_empty``-style assertions see stale data.
    """
    if db.backend != Backend.POSTGRES:
        return
    db.execute("DELETE FROM rca_objects")
    db.execute("DELETE FROM candidate_knowledge")
    db.commit()


def _dbs(tmp_path):
    yield "sqlite", open_db(f"sqlite:///{tmp_path}/r.sqlite3", row_factory="dict")
    if os.getenv("TEST_POSTGRES_URL"):
        pg = open_db(os.environ["TEST_POSTGRES_URL"], row_factory="dict")
        _truncate_rca_objects(pg)
        yield "postgres", pg


def _raw_alarm(alarm_id: str = "a1") -> RawAlarmEvent:
    return RawAlarmEvent(
        alarm_id=alarm_id,
        alarm_code="LINK_DEGRADE",
        alarm_name="link down",
        vendor="huawei",
        site_id="SITE-001",
        cell_id="CELL-1",
        ne_id="NE-1",
        severity="major",
        start_time="2026-06-18T10:00:00+08:00",
        raw_payload={},
    )


def _alarm_event(alarm_id: str = "a1") -> AlarmEvent:
    return AlarmEvent(
        **_raw_alarm(alarm_id).model_dump(),
        alarm_event_id="alarm_evt_001",
        fingerprint="huawei:SITE-001:NE-1:LINK_DEGRADE",
    )


def _incident(incident_id: str = "inc_001") -> IncidentResponse:
    return IncidentResponse(
        incident_id=incident_id,
        title="SITE-001 link down",
        status="analyzing",
        severity="major",
        site_id="SITE-001",
        primary_alarm=_alarm_event(),
        related_alarm_count=0,
        alarm_events=[_alarm_event()],
    )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def test_build_rca_store_defaults_to_sqlite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = build_rca_store(db_path=str(tmp_path / "r.sqlite3"))
    assert isinstance(store, SQLiteRcaStore)


def test_build_rca_store_sqlite_url(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/r.sqlite3")
    store = build_rca_store()
    assert isinstance(store, SQLiteRcaStore)


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_build_rca_store_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
    store = build_rca_store()
    assert isinstance(store, PgRcaStore)
    assert store.backend == Backend.POSTGRES


# --------------------------------------------------------------------------- #
# RCA persistence contract (portable across backends)
# --------------------------------------------------------------------------- #


def test_rca_save_and_load_alarm(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgRcaStore(db=db)
        store.init_schema()
        store.save_alarm(_alarm_event())
        loaded = store.load_alarms()
        assert "alarm_evt_001" in loaded
        assert loaded["alarm_evt_001"].alarm_code == "LINK_DEGRADE"


def test_rca_save_and_load_incident(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgRcaStore(db=db)
        store.init_schema()
        store.save_incident(_incident())
        loaded = store.load_incidents()
        assert "inc_001" in loaded
        assert loaded["inc_001"].title == "SITE-001 link down"


def test_rca_upsert_is_idempotent(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgRcaStore(db=db)
        store.init_schema()
        store.save_alarm(_alarm_event())
        store.save_alarm(_alarm_event())  # same id → upsert, no dupe
        assert len(store.load_alarms()) == 1


def test_rca_save_run_and_report(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgRcaStore(db=db)
        store.init_schema()
        run = RcaRunResponse(
            run_id="rca_run_001",
            incident_id="inc_001",
            report_id="rca_report_001",
            status="accepted",
            current_node="HumanReview",
            trace_id="trace-1",
            state_history=["AlarmReceived", "HumanReview"],
            evidence_count=0,
            evidence=[],
            hypotheses=[],
        )
        report = RcaReportResponse(
            report_id="rca_report_001",
            run_id="rca_run_001",
            incident_id="inc_001",
            report_markdown="# RCA",
            hypotheses=[],
            evidence=[],
            review_status="accepted",
        )
        store.save_run(run)
        store.save_report(report)
        assert "rca_run_001" in store.load_runs()
        assert "rca_report_001" in store.load_reports()


def test_rca_load_empty_returns_empty(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgRcaStore(db=db)
        store.init_schema()
        assert store.load_alarms() == {}
        assert store.load_incidents() == {}


# --------------------------------------------------------------------------- #
# Portability proof: PgRcaStore runs on a SQLite DB
# --------------------------------------------------------------------------- #


def test_pg_rca_store_runs_against_sqlite(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/r.sqlite3", row_factory="dict")
    store = PgRcaStore(db=db)
    assert store.backend == Backend.SQLITE
    store.init_schema()
    store.save_alarm(_alarm_event())
    assert "alarm_evt_001" in store.load_alarms()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_pg_rca_store_runs_against_live_postgres() -> None:
    db = open_db(os.environ["TEST_POSTGRES_URL"], row_factory="dict")
    store = PgRcaStore(db=db)
    assert store.backend == Backend.POSTGRES
    store.init_schema()
    _truncate_rca_objects(db)
    store.save_alarm(_alarm_event("pg-alarm"))
    assert any(a.alarm_id == "pg-alarm" for a in store.load_alarms().values())
