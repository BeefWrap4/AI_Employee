"""SQLite backup/restore utility tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ai_employee.common_schemas.sqlite_backup import (
    BackupResult,
    backup_sqlite,
    restore_sqlite,
    verify_sqlite,
)


def _seed_db(path: Path, *, rows: int = 5) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        for i in range(rows):
            conn.execute("INSERT INTO items (id, name) VALUES (?, ?)", (i, f"name_{i}"))
        conn.commit()
    finally:
        conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM items")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# backup_sqlite
# --------------------------------------------------------------------------- #


def test_backup_sqlite_creates_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.sqlite3"
    _seed_db(src, rows=3)
    dest = tmp_path / "backup.sqlite3"
    result = backup_sqlite(src, dest)
    assert isinstance(result, BackupResult)
    assert result.bytes_written > 0
    assert dest.exists()
    # Backup is a working database with the same rows.
    assert _row_count(dest) == 3


def test_backup_sqlite_returns_metadata(tmp_path: Path) -> None:
    src = tmp_path / "src.sqlite3"
    _seed_db(src, rows=10)
    dest = tmp_path / "backup.sqlite3"
    result = backup_sqlite(src, dest)
    assert result.source_path == str(src)
    assert result.dest_path == str(dest)
    assert result.duration_ms >= 0
    assert result.page_count >= 1


def test_backup_sqlite_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_sqlite(tmp_path / "missing.sqlite3", tmp_path / "out.sqlite3")


def test_backup_sqlite_destination_dir_must_exist(tmp_path: Path) -> None:
    src = tmp_path / "src.sqlite3"
    _seed_db(src, rows=2)
    missing_dir = tmp_path / "no-such-dir" / "out.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_sqlite(src, missing_dir)


# --------------------------------------------------------------------------- #
# restore_sqlite
# --------------------------------------------------------------------------- #


def test_restore_sqlite_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src.sqlite3"
    _seed_db(src, rows=7)
    backup = tmp_path / "backup.sqlite3"
    backup_sqlite(src, backup)

    restore_dest = tmp_path / "restored.sqlite3"
    restore_sqlite(backup, restore_dest)
    assert _row_count(restore_dest) == 7


def test_restore_sqlite_overwrites_existing(tmp_path: Path) -> None:
    backup = tmp_path / "backup.sqlite3"
    src = tmp_path / "src.sqlite3"
    _seed_db(src, rows=4)
    backup_sqlite(src, backup)

    target = tmp_path / "target.sqlite3"
    _seed_db(target, rows=99)  # has different data
    assert _row_count(target) == 99
    restore_sqlite(backup, target)
    # Now matches the backup, not the original.
    assert _row_count(target) == 4


def test_restore_sqlite_missing_backup_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        restore_sqlite(tmp_path / "missing.sqlite3", tmp_path / "out.sqlite3")


# --------------------------------------------------------------------------- #
# verify_sqlite
# --------------------------------------------------------------------------- #


def test_verify_sqlite_returns_ok_for_valid_db(tmp_path: Path) -> None:
    db = tmp_path / "good.sqlite3"
    _seed_db(db, rows=3)
    assert verify_sqlite(db) is True


def test_verify_sqlite_returns_false_for_corrupted_db(tmp_path: Path) -> None:
    db = tmp_path / "bad.sqlite3"
    db.write_bytes(b"this is not a sqlite file at all")
    assert verify_sqlite(db) is False


def test_verify_sqlite_returns_false_for_missing_file(tmp_path: Path) -> None:
    assert verify_sqlite(tmp_path / "missing.sqlite3") is False
