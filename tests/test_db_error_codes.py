import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from ai_employee.knowledge_api.store import SQLiteStore
from fastapi import HTTPException


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    s.init_schema()
    return s


def test_db_locked_retries_then_raises(store: SQLiteStore) -> None:
    """OperationalError 含 'locked' → 重试 3 次后 500 db_locked。"""
    calls = {"n": 0}

    def fake_connect(self):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with (
        patch.object(SQLiteStore, "_connect", fake_connect),
        patch("ai_employee.knowledge_api.store.time.sleep", lambda *_: None),
    ):
        with pytest.raises(HTTPException) as exc:
            store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    assert exc.value.status_code == 500
    assert exc.value.detail["error_code"] == "db_locked"
    # range(3) = 3 次尝试
    assert calls["n"] == 3


def test_other_operational_error_raises_db_write_failed(store: SQLiteStore) -> None:
    calls = {"n": 0}

    def fake_connect(self):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    with patch.object(SQLiteStore, "_connect", fake_connect):
        with pytest.raises(HTTPException) as exc:
            store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    assert exc.value.status_code == 500
    assert exc.value.detail["error_code"] == "db_write_failed"
    # 非 locked 类错误不重试
    assert calls["n"] == 1


def test_integrity_error_raises_db_write_failed(store: SQLiteStore) -> None:
    """IntegrityError（UNIQUE 冲突）→ db_write_failed。"""
    # 先建一个 doc
    store.create_document("A", "/tmp/x", "text/plain", {}, [], "v1")

    # 触发 UNIQUE 冲突：手工插入
    def fake_execute(*a, **kw):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: documents.doc_id")

    import sqlite3 as _sq

    real_connect = SQLiteStore._connect

    def fake_connect(self):
        ctx = real_connect(self)

        class _Wrap:
            def __init__(self, c):
                self._c = c

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self._c.close()
                return False  # 不抑制异常

            def execute(self, sql, *args, **kwargs):
                if "INSERT INTO documents" in sql:
                    raise _sq.IntegrityError("UNIQUE constraint failed: documents.doc_id")
                return self._c.execute(sql, *args, **kwargs)

            def commit(self):
                self._c.commit()

        return _Wrap(ctx)

    with patch.object(SQLiteStore, "_connect", fake_connect):
        with pytest.raises(HTTPException) as exc:
            store.create_document("B", "/tmp/x", "text/plain", {}, [], "v1")
    assert exc.value.status_code == 500
    assert exc.value.detail["error_code"] == "db_write_failed"


def test_successful_write_no_retry(store: SQLiteStore) -> None:
    """正常写入不重试。"""
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    assert doc_id == "doc_001"


def test_read_methods_not_wrapped(store: SQLiteStore) -> None:
    """读方法不包装饰器——读错误不强制 error_code。"""
    # get_document 正常调用不应抛 HTTPException
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, [], "v1")
    doc = store.get_document(doc_id)
    assert doc["title"] == "SOP"
