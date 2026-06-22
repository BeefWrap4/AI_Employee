"""SQLite backup / restore / verify helpers (spec §8.2).

Wraps the built-in ``sqlite3`` connection backup API
(``Connection.backup``) which streams pages one at a time — safe to
call on a live, in-use database and robust against partial writes.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BackupResult:
    source_path: str
    dest_path: str
    bytes_written: int
    page_count: int
    duration_ms: float


def backup_sqlite(source: str | Path, dest: str | Path) -> BackupResult:
    """Copy ``source`` SQLite database to ``dest`` atomically.

    Uses the connection backup API so the source database may remain
    open by other connections during the copy.  Raises
    :class:`FileNotFoundError` if the source doesn't exist or the
    destination directory is missing.
    """
    source_path = Path(source)
    dest_path = Path(dest)
    if not source_path.exists():
        raise FileNotFoundError(f"source database not found: {source_path}")
    if not dest_path.parent.exists():
        raise FileNotFoundError(f"destination directory missing: {dest_path.parent}")

    started = time.perf_counter()
    src_conn = sqlite3.connect(str(source_path))
    try:
        # ``backup`` returns when the copy is complete and reports the
        # remaining page count (0 means fully copied).
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            with dest_conn:
                src_conn.backup(dest_conn)
            page_count = dest_conn.execute("PRAGMA page_count").fetchone()[0]
            bytes_written = source_path.stat().st_size  # source size as a proxy
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    duration_ms = (time.perf_counter() - started) * 1000.0
    return BackupResult(
        source_path=str(source_path),
        dest_path=str(dest_path),
        bytes_written=bytes_written,
        page_count=int(page_count),
        duration_ms=duration_ms,
    )


def restore_sqlite(backup: str | Path, dest: str | Path) -> BackupResult:
    """Restore ``backup`` to ``dest``, overwriting any existing database.

    The destination is overwritten via the same backup API, so the
    restore is safe to run on a live deployment target.
    """
    return backup_sqlite(backup, dest)


def verify_sqlite(path: str | Path) -> bool:
    """Return True if ``path`` is a readable, non-corrupt SQLite database.

    Uses SQLite's built-in ``PRAGMA integrity_check`` which scans the
    whole file for structural damage.
    """
    p = Path(path)
    if not p.exists():
        return False
    try:
        conn = sqlite3.connect(str(p))
    except sqlite3.DatabaseError:
        return False
    try:
        cur = conn.execute("PRAGMA integrity_check")
        row = cur.fetchone()
        # integrity_check returns a single row whose first column is "ok"
        # when the database is healthy.
        return bool(row) and str(row[0]).lower() == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


__all__ = [
    "BackupResult",
    "backup_sqlite",
    "restore_sqlite",
    "verify_sqlite",
]
