"""Database connection abstraction (R16-1).

A thin ``DB`` wrapper lets the platform's stores run the same SQL against
both SQLite (dev/test default) and PostgreSQL (prod, spec-mandated).
The wrapper normalises the placeholder style (``?`` for sqlite3,
``%s`` for psycopg) so store code stays backend-agnostic — stores write
``?``-style SQL and the wrapper rewrites it for Postgres at execute time.

Env:
  DATABASE_URL — ``postgres://...`` / ``postgresql://...`` selects PG;
                 ``sqlite:///path`` or unset selects SQLite (default).

The wrapper deliberately does NOT introduce an ORM.  The existing stores
already use hand-written SQL; this keeps their SQL intact while making
the backend swappable.  An ORM migration is a future concern.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Backend(str, Enum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


_POSTGRES_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg"}


def detect_backend(url: str | None) -> Backend:
    """Infer the backend from a database URL.

    ``None`` / empty / ``sqlite://...`` → SQLite.
    ``postgres://...`` / ``postgresql://...`` → Postgres.
    Anything else raises :class:`ValueError` (fail fast on a typo).
    """
    if not url:
        return Backend.SQLITE
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme in _POSTGRES_SCHEMES:
        return Backend.POSTGRES
    if scheme in {"sqlite", "sqlite3"}:
        return Backend.SQLITE
    raise ValueError(
        f"unsupported database scheme: {scheme or url!r}; expected sqlite:/// or postgres://",
    )


@dataclass
class DatabaseConfig:
    url: str
    backend: Backend
    pool_size: int = 5


def build_database_config(database_url: str | None = None) -> DatabaseConfig:
    """Build a :class:`DatabaseConfig` from env (``DATABASE_URL``)."""
    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    backend = detect_backend(url)
    if not url:
        url = "sqlite:///./var/data/app.sqlite3"
    return DatabaseConfig(url=url, backend=backend)


# --------------------------------------------------------------------------- #
# Placeholder translation
# --------------------------------------------------------------------------- #

# Matches ``?`` that are NOT inside a SQL string literal.  We track
# single-quote state so a literal ``'is this ok?'`` is left alone.
_QUESTION_RE = re.compile(r"(?P<q>\?)")


def translate_placeholders(sql: str, backend: Backend) -> str:
    """Rewrite ``?`` placeholders to ``%s`` for Postgres.

    SQLite keeps ``?``.  A ``?`` inside a single-quoted string literal
    is preserved (the regex scanner toggles in/out of literal state).
    """
    if backend == Backend.SQLITE:
        return sql
    out: list[str] = []
    in_literal = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_literal = not in_literal
            out.append(ch)
        elif ch == "?" and not in_literal:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# DB wrapper
# --------------------------------------------------------------------------- #


class DB:
    """Unified execute surface over sqlite3 / psycopg connections.

    Stores call ``db.execute(sql, params)`` with ``?`` placeholders; the
    wrapper rewrites them for Postgres.  ``fetchall``/``fetchone`` proxy
    to the underlying cursor.  Thread-safe via a lock (SQLite is
    single-writer; psycopg connections are also not thread-safe to share).

    When ``row_factory="dict"`` is set, :meth:`execute` returns a
    :class:`_DictCursor` whose ``fetchone``/``fetchall`` yield dicts
    keyed by column name — mirroring ``sqlite3.Row`` access-by-name so
    store code stays identical across backends.
    """

    def __init__(
        self,
        conn: Any,
        backend: Backend,
        *,
        row_factory: str = "tuple",
    ) -> None:
        self._conn = conn
        self.backend = backend
        self.row_factory = row_factory
        self._lock = threading.Lock()

    # -- connection management ------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -- DDL/DML --------------------------------------------------------------
    def execute(self, sql: str, params: tuple | list | None = None) -> Any:
        translated = translate_placeholders(sql, self.backend)
        with self._lock:
            cur = self._conn.cursor()
            if params is None:
                cur.execute(translated)
            else:
                cur.execute(translated, params)
            if self.row_factory == "dict":
                return _DictCursor(cur)
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        translated = translate_placeholders(sql, self.backend)
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(translated, seq)
            return cur

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            try:
                self._conn.rollback()
            except Exception:
                pass

    # -- transaction context manager -----------------------------------------
    def transaction(self) -> _Transaction:
        return _Transaction(self)


class _DictCursor:
    """Wraps a raw cursor so rows come back as dicts keyed by column name.

    Mirrors ``sqlite3.Row`` access-by-name (``row["col"]``) so store code
    that indexes rows by column name works unchanged on both backends.
    """

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    @property
    def description(self) -> Any:
        return self._cur.description

    @property
    def lastrowid(self) -> Any:
        return getattr(self._cur, "lastrowid", None)

    @property
    def rowcount(self) -> int:
        return getattr(self._cur, "rowcount", -1)

    def _cols(self) -> list[str]:
        desc = self._cur.description
        if not desc:
            return []
        # sqlite3 description rows are tuples; psycopg returns Column objects
        # with a ``.name`` attr.  Handle both.
        cols: list[str] = []
        for col in desc:
            if isinstance(col, str):
                cols.append(col)
            else:
                cols.append(
                    col[0] if isinstance(col, (tuple, list)) else getattr(col, "name", str(col))
                )
        return cols

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = self._cols()
        if cols:
            return dict(zip(cols, row, strict=False))
        return dict(enumerate(row)) if row else None

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self._cur.fetchall()
        cols = self._cols()
        if cols:
            return [dict(zip(cols, r, strict=False)) for r in rows]
        return [dict(enumerate(r)) if r else {} for r in rows]


class _Transaction:
    """``with db.transaction(): ...`` commits on success, rolls back on error."""

    def __init__(self, db: DB) -> None:
        self._db = db

    def __enter__(self) -> DB:
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is None:
            self._db.commit()
        else:
            self._db.rollback()


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def open_db(url: str | None = None, *, row_factory: str = "tuple") -> DB:
    """Open a :class:`DB` for the given URL (or ``DATABASE_URL`` env).

    SQLite connections are opened with ``check_same_thread=False`` so the
    wrapper's lock can serialise access from request threads.  Postgres
    connections use psycopg's blocking connect.  Pass ``row_factory="dict"``
    to get dict-keyed rows (mirrors ``sqlite3.Row`` access-by-name).
    """
    cfg = build_database_config(url)
    if cfg.backend == Backend.SQLITE:
        path = _sqlite_path(cfg.url)
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        conn.row_factory = None  # tuple rows by default (stores index by position)
        return DB(conn, Backend.SQLITE, row_factory=row_factory)
    # Postgres
    import psycopg  # type: ignore[import-untyped]

    pg_url = _normalise_postgres_url(cfg.url)
    conn = psycopg.connect(pg_url, autocommit=False)
    return DB(conn, Backend.POSTGRES, row_factory=row_factory)


def _sqlite_path(url: str) -> str:
    """``sqlite:///./var/x.db`` → ``./var/x.db``; ``:memory:`` → ``''``.

    Handles Windows drive paths where ``sqlite:///C:\\...`` would otherwise
    leave a leading ``/`` (``/C:\\...``) that ``os.makedirs`` rejects.
    """
    if "://" not in url:
        return url
    path = url.split("://", 1)[1]
    if path == ":memory:":
        return ""
    # ``sqlite:///C:\\...`` (Windows) → strip the leading ``/`` before a drive letter.
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path


def _normalise_postgres_url(url: str) -> str:
    """psycopg accepts ``postgresql://`` (and ``postgres://`` via alias)."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


__all__ = [
    "DB",
    "Backend",
    "DatabaseConfig",
    "build_database_config",
    "detect_backend",
    "open_db",
    "translate_placeholders",
]
