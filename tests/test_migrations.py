"""Alembic migration round-trip tests.

Verifies the baseline migration applies cleanly to a fresh SQLite DB,
creates every expected table, and that ``downgrade`` removes them.
Runs Alembic programmatically (no shell) so the test works in CI.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlite3

alembic = pytest.importorskip("alembic", reason="alembic required")
from alembic import command
from alembic.config import Config


REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _tables(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


EXPECTED_TABLES = {
    "documents", "chunks", "chunks_fts", "qa_logs", "feedbacks",
    "rca_objects", "candidate_knowledge",
    "agent_runs", "agent_run_events", "eval_runs",
    "tools",
}


def test_baseline_upgrade_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.sqlite3"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")
    tables = _tables(str(db_path))
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables after upgrade: {sorted(missing)}"


def test_baseline_upgrade_is_idempotent_against_pre_existing_schema(tmp_path: Path) -> None:
    """If a store already bootstrapped the table, the migration must not fail."""
    db_path = tmp_path / "mig.sqlite3"
    # Pre-create one table the way the store would.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE documents (doc_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "source_uri TEXT NOT NULL, mime_type TEXT NOT NULL, metadata_json TEXT NOT NULL, "
        "acl_tags_json TEXT NOT NULL, parse_status TEXT NOT NULL, parse_error TEXT, "
        "chunk_count INTEGER NOT NULL DEFAULT 0, version TEXT NOT NULL DEFAULT 'v1', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    db_url = f"sqlite:///{db_path}"
    # Should not raise on the already-existing table.
    command.upgrade(_alembic_config(db_url), "head")
    assert "documents" in _tables(str(db_path))


def test_downgrade_removes_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.sqlite3"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(db_url), "head")
    command.downgrade(_alembic_config(db_url), "base")
    tables = _tables(str(db_path))
    leftover = EXPECTED_TABLES & tables
    assert not leftover, f"tables still present after downgrade: {sorted(leftover)}"


def test_upgrade_then_downgrade_then_upgrade(tmp_path: Path) -> None:
    """Full cycle: the DB ends in the upgraded state again."""
    db_path = tmp_path / "mig.sqlite3"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES <= _tables(str(db_path))


def test_current_revision_is_head(tmp_path: Path) -> None:
    """After upgrade, ``alembic current`` reports the baseline revision."""
    db_path = tmp_path / "mig.sqlite3"
    db_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    # The alembic_version table holds the active revision.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "0001_baseline"
