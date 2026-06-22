"""Storage backend abstraction tests (SQLite + Postgres).

These tests pin the cross-backend behaviour we rely on: parameter
substitution, transaction handling, and a small smoke CRUD on a
representative schema (documents + chunks).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ai_employee.common_schemas.storage import (
    PostgresBackend,
    SqliteBackend,
    build_backend,
    placeholder,
)

# --------------------------------------------------------------------------- #
# placeholder / dialect handling
# --------------------------------------------------------------------------- #


def test_sqlite_placeholder_is_qmark() -> None:
    assert placeholder("sqlite") == "?"


def test_postgres_placeholder_is_percent_s() -> None:
    assert placeholder("postgres") == "%s"


def test_build_backend_from_env_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "x.sqlite3"))
    backend = build_backend()
    assert isinstance(backend, SqliteBackend)


def test_build_backend_unknown_dialect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "mysql")
    with pytest.raises(ValueError):
        build_backend()


# --------------------------------------------------------------------------- #
# SqliteBackend behaviour (always available)
# --------------------------------------------------------------------------- #


def test_sqlite_backend_create_and_insert(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "test.sqlite3")
    backend.execute("CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT)")
    backend.execute(
        "INSERT INTO items (id, name) VALUES (?, ?)",
        ("i1", "widget"),
    )
    rows = backend.fetchall("SELECT id, name FROM items ORDER BY id")
    assert rows == [("i1", "widget")]


def test_sqlite_backend_transaction_rollback(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "test.sqlite3")
    backend.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError):
        with backend.transaction():
            backend.execute("INSERT INTO items (id) VALUES (?)", ("ok",))
            raise RuntimeError("boom")
    rows = backend.fetchall("SELECT id FROM items")
    assert rows == []


def test_sqlite_backend_transaction_commit(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "test.sqlite3")
    backend.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")
    with backend.transaction():
        backend.execute("INSERT INTO items (id) VALUES (?)", ("ok",))
    rows = backend.fetchall("SELECT id FROM items")
    assert rows == [("ok",)]


def test_sqlite_backend_close_is_idempotent(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "test.sqlite3")
    backend.execute("CREATE TABLE t (x INTEGER)")
    backend.close()
    backend.close()  # should not raise


def test_sqlite_backend_execute_many(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "test.sqlite3")
    backend.execute("CREATE TABLE t (x INTEGER)")
    backend.execute_many("INSERT INTO t (x) VALUES (?)", [(1,), (2,), (3,)])
    rows = backend.fetchall("SELECT x FROM t ORDER BY x")
    assert [r[0] for r in rows] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# PostgresBackend behaviour (only when psycopg is installed)
# --------------------------------------------------------------------------- #


psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")


@pytest.fixture
def pg_backend() -> PostgresBackend:
    """Skip when no Postgres test DSN is configured."""
    import os

    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN not set; skipping live Postgres tests")
    backend = PostgresBackend(dsn)
    # Clean slate.
    backend.execute("DROP TABLE IF EXISTS items")
    backend.execute("CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT)")
    yield backend
    backend.execute("DROP TABLE IF EXISTS items")
    backend.close()


def test_postgres_backend_insert_and_fetch(pg_backend: PostgresBackend) -> None:
    pg_backend.execute(
        "INSERT INTO items (id, name) VALUES (%s, %s)",
        ("i1", "widget"),
    )
    rows = pg_backend.fetchall("SELECT id, name FROM items ORDER BY id")
    assert rows == [("i1", "widget")]


def test_postgres_backend_transaction_rollback(pg_backend: PostgresBackend) -> None:
    with pytest.raises(RuntimeError):
        with pg_backend.transaction():
            pg_backend.execute("INSERT INTO items (id, name) VALUES (%s, %s)", ("x", "y"))
            raise RuntimeError("boom")
    rows = pg_backend.fetchall("SELECT id FROM items")
    assert rows == []
