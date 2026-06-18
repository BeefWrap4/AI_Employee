"""Postgres dialect compatibility for the baseline migration (R16-2).

The migration must not emit SQLite-only constructs when run against
PostgreSQL.  We exercise this two ways:

1. **Static branch check** — replay ``upgrade()`` against a fake ``op``
   whose bind reports ``postgresql`` and assert no SQLite-only tokens
   (``fts5``, ``AUTOINCREMENT``, ``CREATE TRIGGER ... BEGIN``) leak
   into the recorded SQL.
2. **Live round-trip** (gated on ``TEST_POSTGRES_URL``) — apply the
   migration to a real Postgres, confirm the expected tables exist,
   then downgrade.
"""
from __future__ import annotations

import os
import re

import pytest

alembic = pytest.importorskip("alembic", reason="alembic required")


# --------------------------------------------------------------------------- #
# Fake op that records every SQL statement + reports a chosen dialect
# --------------------------------------------------------------------------- #


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = _FakeDialect(dialect_name)


class _FakeOp:
    def __init__(self, dialect_name: str) -> None:
        self._dialect = dialect_name
        self.statements: list[str] = []

    def get_bind(self) -> _FakeBind:
        return _FakeBind(self._dialect)

    def execute(self, sql: str) -> None:
        self.statements.append(sql)


_SQLITE_ONLY_TOKENS = [
    (re.compile(r"\bfts5\b", re.IGNORECASE), "fts5 virtual table"),
    (re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE), "AUTOINCREMENT"),
    (re.compile(r"CREATE\s+TRIGGER.*BEGIN", re.IGNORECASE | re.DOTALL), "SQLite trigger BEGIN block"),
    (re.compile(r"CREATE\s+VIRTUAL\s+TABLE", re.IGNORECASE), "CREATE VIRTUAL TABLE"),
]


def _run_upgrade_against(dialect: str) -> list[str]:
    """Replay the baseline migration against a fake op with the given dialect."""
    import importlib

    fake = _FakeOp(dialect)
    # Patch alembic.op inside the migration module.
    import alembic.op as real_op

    mod = importlib.import_module("migrations.versions.0001_baseline_schemas")
    original = mod.op
    mod.op = fake  # type: ignore[assignment]
    try:
        mod.upgrade()
    finally:
        mod.op = original  # type: ignore[assignment]
        # Ensure real alembic.op is untouched for other tests.
        del real_op
    return fake.statements


# --------------------------------------------------------------------------- #
# Static branch checks
# --------------------------------------------------------------------------- #


def test_postgres_branch_has_no_sqlite_only_tokens() -> None:
    stmts = _run_upgrade_against("postgresql")
    joined = "\n".join(stmts)
    for pattern, label in _SQLITE_ONLY_TOKENS:
        match = pattern.search(joined)
        assert match is None, (
            f"Postgres branch emitted SQLite-only construct {label!r}: "
            f"{match.group(0)!r}" if match else ""
        )


def test_postgres_branch_uses_identity_for_event_id() -> None:
    stmts = _run_upgrade_against("postgresql")
    joined = "\n".join(stmts)
    assert "GENERATED ALWAYS AS IDENTITY" in joined
    assert "BIGINT" in joined


def test_postgres_branch_omits_fts_table_and_triggers() -> None:
    stmts = _run_upgrade_against("postgresql")
    joined = "\n".join(stmts)
    assert "chunks_fts" not in joined
    assert "chunks_ai" not in joined
    assert "chunks_ad" not in joined


def test_postgres_branch_still_creates_core_tables() -> None:
    stmts = _run_upgrade_against("postgresql")
    joined = "\n".join(stmts)
    for table in (
        "documents", "chunks", "qa_logs", "feedbacks",
        "rca_objects", "candidate_knowledge",
        "agent_runs", "agent_run_events", "eval_runs", "tools",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined, (
            f"Postgres branch missing CREATE TABLE for {table}"
        )


def test_sqlite_branch_still_uses_fts5() -> None:
    """The SQLite path must keep its FTS5 index (regression guard)."""
    stmts = _run_upgrade_against("sqlite")
    joined = "\n".join(stmts)
    assert "USING fts5" in joined
    assert "AUTOINCREMENT" in joined
    assert "CREATE TRIGGER" in joined


def test_sqlite_branch_does_not_use_identity() -> None:
    """SQLite path must not emit Postgres IDENTITY columns."""
    stmts = _run_upgrade_against("sqlite")
    joined = "\n".join(stmts)
    assert "GENERATED ALWAYS AS IDENTITY" not in joined


# --------------------------------------------------------------------------- #
# Live Postgres round-trip (gated)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_baseline_applies_to_live_postgres() -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[1]
    url = os.environ["TEST_POSTGRES_URL"]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    # Clean slate in case a prior run left tables.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    import psycopg  # type: ignore[import-untyped]

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        tables = {r[0] for r in rows}
    for expected in (
        "documents", "chunks", "qa_logs", "feedbacks",
        "rca_objects", "candidate_knowledge",
        "agent_runs", "agent_run_events", "eval_runs", "tools",
    ):
        assert expected in tables, f"missing {expected} in live Postgres"
    # FTS table must NOT exist on Postgres.
    assert "chunks_fts" not in tables
    command.downgrade(cfg, "base")
