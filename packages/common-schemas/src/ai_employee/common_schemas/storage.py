"""Storage backend abstraction (spec §8).

Defines a thin protocol so the same business code can run against
SQLite (dev / single-node) or Postgres (prod / multi-node).  The
abstraction is intentionally small:

* :meth:`execute` — single statement with positional ``?`` or ``%s``
  params (placeholder returned by :func:`placeholder`).
* :meth:`execute_many` — bulk insert helper.
* :meth:`fetchall` — return all rows as tuples.
* :meth:`transaction` — context manager with commit/rollback.

The two concrete implementations are :class:`SqliteBackend` and
:class:`PostgresBackend`.  :func:`build_backend` selects one based on
the ``STORAGE_BACKEND`` env var (``sqlite`` default, ``postgres``
alternative).

Postgres support is lazy: :class:`PostgresBackend` imports
``psycopg2`` only when instantiated, so the package stays importable
in environments that don't have it installed.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol


class StorageBackend(Protocol):
    """Minimal contract every backend must satisfy."""

    dialect: str

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None: ...

    def execute_many(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None: ...

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]: ...

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def close(self) -> None: ...


def placeholder(dialect: str) -> str:
    """Return the parameter placeholder for the given dialect.

    SQLite uses ``?``; Postgres uses ``%s``.  All concrete backends
    normalise user input through this helper so SQL strings can be
    written once.
    """
    if dialect == "sqlite":
        return "?"
    if dialect == "postgres":
        return "%s"
    raise ValueError(f"unsupported dialect: {dialect!r}")


class SqliteBackend:
    """Process-local SQLite backend with WAL + FK enforcement."""

    dialect = "sqlite"

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 30.0,
        row_factory: Any = None,
    ) -> None:
        self.path = str(path)
        # ``isolation_level=None`` puts psycopg/sqlite3 in autocommit mode
        # so the transaction() context can BEGIN/COMMIT explicitly.
        self._conn = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        if row_factory is not None:
            self._conn.row_factory = row_factory
        # Foreign keys + WAL — match the per-service settings used in M0.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        cur = self._conn.execute(sql, params)
        cur.close()

    def execute_many(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None:
        cur = self._conn.executemany(sql, seq_of_params)
        cur.close()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        cur = self._conn.execute(sql, params)
        try:
            return list(cur.fetchall())
        finally:
            cur.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.ProgrammingError:
            pass


class PostgresBackend:
    """Postgres backend backed by psycopg (v3) with a psycopg2 fallback.

    Tries ``psycopg`` first (the v3 module declared in pyproject), then
    falls back to ``psycopg2`` when only the legacy driver is
    installed.  Both expose the same ``connection``/``cursor`` API so
    the body of the methods is shared.
    """

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        conn = None
        try:
            import psycopg  # type: ignore[import-not-found]

            conn = psycopg.connect(dsn, autocommit=False)
        except ImportError:
            try:
                import psycopg2  # type: ignore[import-not-found]

                conn = psycopg2.connect(dsn)
                conn.autocommit = False
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "psycopg (v3) or psycopg2 is required for PostgresBackend; "
                    "install with `pip install 'psycopg[binary]'`",
                ) from exc
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)

    def execute_many(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(sql, seq_of_params)

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def build_backend(
    *,
    backend: str | None = None,
    sqlite_path: str | Path | None = None,
    postgres_dsn: str | None = None,
) -> StorageBackend:
    """Pick a backend from explicit args or env vars.

    Env vars consumed:

    * ``STORAGE_BACKEND`` — ``sqlite`` (default) or ``postgres``.
    * ``SQLITE_PATH`` — used when backend is ``sqlite``.
    * ``POSTGRES_DSN`` — used when backend is ``postgres``.
    """
    chosen = (backend or os.environ.get("STORAGE_BACKEND", "sqlite")).lower()
    if chosen == "sqlite":
        path = sqlite_path or os.environ.get("SQLITE_PATH", ":memory:")
        return SqliteBackend(path)
    if chosen == "postgres":
        dsn = postgres_dsn or os.environ.get("POSTGRES_DSN", "")
        if not dsn:
            raise ValueError("POSTGRES_DSN is required for postgres backend")
        return PostgresBackend(dsn)
    raise ValueError(f"unsupported STORAGE_BACKEND: {chosen!r}")


__all__ = [
    "PostgresBackend",
    "SqliteBackend",
    "StorageBackend",
    "build_backend",
    "placeholder",
]
