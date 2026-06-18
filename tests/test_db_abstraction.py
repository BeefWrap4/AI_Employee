"""Database connection abstraction tests (R16-1).

A thin ``DB`` wrapper lets the platform's stores run the same SQL against
both SQLite (dev/test default) and PostgreSQL (prod, spec-mandated).
The wrapper normalises the placeholder style (``?`` for sqlite3,
``%s`` for psycopg) so store code stays backend-agnostic.

Env:
  DATABASE_URL — ``postgres://...`` or ``postgresql://...`` selects PG;
                  ``sqlite:///path`` or unset selects SQLite.
"""
from __future__ import annotations

import os

import pytest
from ai_employee.common_schemas.db import (
    DB,
    Backend,
    DatabaseConfig,
    build_database_config,
    detect_backend,
    open_db,
)

# --------------------------------------------------------------------------- #
# Backend detection
# --------------------------------------------------------------------------- #


def test_detect_backend_sqlite_default() -> None:
    assert detect_backend(None) == Backend.SQLITE
    assert detect_backend("") == Backend.SQLITE
    assert detect_backend("sqlite:///./var/data/x.sqlite3") == Backend.SQLITE


def test_detect_backend_postgres() -> None:
    for url in [
        "postgres://user:pass@host:5432/db",
        "postgresql://user:pass@host:5432/db",
        "postgresql+psycopg://host/db",
    ]:
        assert detect_backend(url) == Backend.POSTGRES


def test_detect_backend_unknown_raises() -> None:
    with pytest.raises(ValueError) as ei:
        detect_backend("mysql://host/db")
    assert "mysql" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# DatabaseConfig from env
# --------------------------------------------------------------------------- #


def test_build_database_config_defaults_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = build_database_config()
    assert cfg.backend == Backend.SQLITE
    assert cfg.url.startswith("sqlite:///")


def test_build_database_config_postgres_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db:5432/ai_employee")
    cfg = build_database_config()
    assert cfg.backend == Backend.POSTGRES
    assert cfg.url == "postgres://u:p@db:5432/ai_employee"


def test_database_config_has_pool_size() -> None:
    cfg = DatabaseConfig(
        url="sqlite:///./x.db", backend=Backend.SQLITE, pool_size=5,
    )
    assert cfg.pool_size == 5


# --------------------------------------------------------------------------- #
# DB wrapper — SQLite path (always available)
# --------------------------------------------------------------------------- #


def test_open_db_sqlite(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    assert db.backend == Backend.SQLITE
    assert isinstance(db, DB)


def test_db_execute_creates_and_inserts(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    db.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
    )
    db.execute("INSERT INTO t (name) VALUES (?)", ("alice",))
    db.commit()
    rows = db.execute("SELECT name FROM t").fetchall()
    assert [r[0] for r in rows] == ["alice"]


def test_db_executemany(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    db.executemany(
        "INSERT INTO t (name) VALUES (?)",
        [("a",), ("b",), ("c",)],
    )
    db.commit()
    rows = db.execute("SELECT name FROM t ORDER BY name").fetchall()
    assert [r[0] for r in rows] == ["a", "b", "c"]


def test_db_fetchone_returns_none_when_empty(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    assert db.execute("SELECT * FROM t").fetchone() is None


def test_db_context_manager_commits_on_exit(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    with db.transaction():
        db.execute("INSERT INTO t (name) VALUES (?)", ("x",))
    # Re-open to verify persistence.
    db2 = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    rows = db2.execute("SELECT name FROM t").fetchall()
    assert rows == [("x",)]


def test_db_transaction_rolls_back_on_exception(tmp_path) -> None:
    db = open_db(f"sqlite:///{tmp_path}/t.sqlite3")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO t (name) VALUES (?)", ("x",))
            raise RuntimeError("boom")
    rows = db.execute("SELECT name FROM t").fetchall()
    assert rows == []


def test_db_placeholder_translates_for_postgres_shape() -> None:
    """The wrapper rewrites ``?`` → ``%s`` when the backend is Postgres.

    We can't run a live PG here, but we can verify the translation logic
    via the internal helper so a store's ``?``-style SQL is portable.
    """
    from ai_employee.common_schemas.db import translate_placeholders

    assert translate_placeholders(
        "INSERT INTO t (a, b) VALUES (?, ?)", Backend.POSTGRES,
    ) == "INSERT INTO t (a, b) VALUES (%s, %s)"
    # SQLite keeps ``?``.
    assert translate_placeholders(
        "INSERT INTO t (a) VALUES (?)", Backend.SQLITE,
    ) == "INSERT INTO t (a) VALUES (?)"


def test_db_translate_does_not_touch_escaped_question_marks() -> None:
    """A literal ``?`` inside a string literal must not be rewritten."""
    from ai_employee.common_schemas.db import translate_placeholders

    sql = "INSERT INTO t (q) VALUES ('is this ok?')"
    assert translate_placeholders(sql, Backend.POSTGRES) == sql


# --------------------------------------------------------------------------- #
# Postgres path — only when a live test DB is available
# --------------------------------------------------------------------------- #


def test_open_db_postgres_skips_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without TEST_POSTGRES_URL the PG smoke is skipped (no live DB in CI)."""
    monkeypatch.delenv("TEST_POSTGRES_URL", raising=False)
    pytest.skip("TEST_POSTGRES_URL not set; skipping live Postgres smoke")


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set",
)
def test_db_postgres_round_trip() -> None:
    url = os.environ["TEST_POSTGRES_URL"]
    db = open_db(url)
    assert db.backend == Backend.POSTGRES
    # Use a unique table name to avoid collisions across test runs.
    table = "r16_smoke"
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(
        f"CREATE TABLE {table} (id SERIAL PRIMARY KEY, name TEXT NOT NULL)",
    )
    db.execute(f"INSERT INTO {table} (name) VALUES (%s)", ("alice",))
    db.commit()
    rows = db.execute(f"SELECT name FROM {table}").fetchall()
    assert [r[0] for r in rows] == ["alice"]
    db.execute(f"DROP TABLE {table}")
    db.commit()
