"""agent-platform AgentRunStore dual-backend tests (R16-4)."""

from __future__ import annotations

import os

import pytest
from ai_employee.agent_platform_api.pg_run_store import PgAgentRunStore, build_run_store
from ai_employee.common_schemas.db import Backend, open_db


def _truncate_run_tables(db) -> None:
    """Clear the shared live-PG run tables so each PG-leg test starts clean.

    The SQLite leg always gets a fresh ``tmp_path`` DB per test; the live PG
    connection is shared across the file, so without this the PG leg sees
    rows left by earlier tests and count-based assertions (``total == 5``,
    ``len(events) == 2``) drift.  SQLite legs are a no-op.
    """
    if db.backend != Backend.POSTGRES:
        return
    db.execute("DELETE FROM agent_run_events")
    db.execute("DELETE FROM agent_runs")
    db.commit()


def _dbs(tmp_path):
    yield "sqlite", open_db(f"sqlite:///{tmp_path}/p.sqlite3", row_factory="dict")
    if os.getenv("TEST_POSTGRES_URL"):
        pg = open_db(os.environ["TEST_POSTGRES_URL"], row_factory="dict")
        _truncate_run_tables(pg)
        yield "postgres", pg


def _run(run_id: str = "run_001", **kw) -> dict:
    base = {
        "run_id": run_id,
        "template_id": "knowledge_qa",
        "agent_name": "Knowledge QA",
        "status": "running",
        "trace_id": "trace-1",
        "requested_by": "alice",
        "input": {"q": "hi"},
        "output": {},
        "node_trace": [{"node_name": "Start", "status": "ok", "detail": ""}],
        "tool_calls": [],
        "approval_status": "not_required",
        "resume_from_node": None,
    }
    base.update(kw)
    return base


def test_build_run_store_defaults_to_sqlite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_employee.agent_platform_api.run_store import AgentRunStore

    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = build_run_store(db_path=str(tmp_path / "p.sqlite3"))
    assert isinstance(store, AgentRunStore)


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_build_run_store_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
    store = build_run_store()
    assert isinstance(store, PgAgentRunStore)
    assert store.backend == Backend.POSTGRES


def test_upsert_and_get_run(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgAgentRunStore(db=db)
        store.init_schema()
        store.upsert_run(_run("run_001"))
        got = store.get_run("run_001")
        assert got is not None
        assert got["run_id"] == "run_001"
        assert got["template_id"] == "knowledge_qa"
        assert got["node_trace"][0]["node_name"] == "Start"
        assert got["events"] == []


def test_upsert_is_idempotent_update(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgAgentRunStore(db=db)
        store.init_schema()
        store.upsert_run(_run("run_001", status="running"))
        store.upsert_run(_run("run_001", status="completed", output={"a": 1}))
        got = store.get_run("run_001")
        assert got["status"] == "completed"
        assert got["output"] == {"a": 1}


def test_append_events(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgAgentRunStore(db=db)
        store.init_schema()
        store.upsert_run(
            {
                **_run("run_001"),
                "new_events": [
                    {"node_name": "N1", "status": "ok", "detail": "d1"},
                    {"node_name": "N2", "status": "ok", "detail": "d2"},
                ],
            }
        )
        got = store.get_run("run_001")
        assert len(got["events"]) == 2
        assert [e["node_name"] for e in got["events"]] == ["N1", "N2"]


def test_list_runs_pagination(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgAgentRunStore(db=db)
        store.init_schema()
        for i in range(5):
            store.upsert_run(_run(f"run_{i:03d}"))
        items, total = store.list_runs(page=1, page_size=2)
        assert total == 5
        assert len(items) == 2


def test_list_runs_filter_by_template(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgAgentRunStore(db=db)
        store.init_schema()
        store.upsert_run(_run("run_a", template_id="knowledge_qa"))
        store.upsert_run(_run("run_b", template_id="rca"))
        items, total = store.list_runs(template_id="rca")
        assert total == 1
        assert items[0]["run_id"] == "run_b"


def test_get_run_not_found_returns_none(tmp_path) -> None:
    for _label, db in _dbs(tmp_path):
        store = PgAgentRunStore(db=db)
        store.init_schema()
        assert store.get_run("nope") is None


def test_pg_run_store_runs_against_sqlite(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/p.sqlite3", row_factory="dict")
    store = PgAgentRunStore(db=db)
    assert store.backend == Backend.SQLITE
    store.init_schema()
    store.upsert_run(_run("run_x"))
    assert store.get_run("run_x") is not None


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_pg_run_store_runs_against_live_postgres() -> None:
    db = open_db(os.environ["TEST_POSTGRES_URL"], row_factory="dict")
    store = PgAgentRunStore(db=db)
    assert store.backend == Backend.POSTGRES
    store.init_schema()
    _truncate_run_tables(db)
    store.upsert_run(_run("run_pg"))
    assert store.get_run("run_pg") is not None
