import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from ai_employee.common_schemas.errors import IndexCorruptedError
from ai_employee.knowledge_api.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    s.init_schema()
    return s


def test_chunks_table_has_acl_tags_json_column(store: SQLiteStore) -> None:
    """spec §5: chunks 表新增 acl_tags_json 列。"""
    with store._lock, store._connect() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    assert "acl_tags_json" in cols


def test_init_schema_idempotent_acl_migration(store: SQLiteStore, tmp_path: Path) -> None:
    """重复 init_schema 不报错（ALTER 不重复加列）。"""
    store.init_schema()  # 第二次
    store.init_schema()  # 第三次
    with store._lock, store._connect() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    assert cols.count("acl_tags_json") == 1


def test_write_chunks_inherits_acl_from_document(store: SQLiteStore) -> None:
    """acl_tags_override=None 时写空列表表示继承 doc（chunk 级过滤跳过）。"""
    doc_id = store.create_document(
        "SOP",
        "/tmp/x",
        "text/plain",
        {"network_type": "5g"},
        ["wireless"],
        "v1",
    )
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    chunk = store.get_chunk(f"c_{doc_id}")
    assert chunk is not None
    # 继承 doc 时 chunk.acl_tags 为空，表示"继承 doc ACL"
    assert chunk["acl_tags"] == []


def test_write_chunks_acl_override(store: SQLiteStore) -> None:
    doc_id = store.create_document(
        "SOP",
        "/tmp/x",
        "text/plain",
        {},
        ["wireless"],
        "v1",
    )
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
        acl_tags_override=["noc", "expert"],
    )
    chunk = store.get_chunk(f"c_{doc_id}")
    assert chunk["acl_tags"] == ["noc", "expert"]


def test_get_chunk_returns_none_for_missing(store: SQLiteStore) -> None:
    assert store.get_chunk("nope") is None


def test_set_chunk_acl_tags_updates(store: SQLiteStore) -> None:
    doc_id = store.create_document("SOP", "/tmp/x", "text/plain", {}, ["wireless"], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    store.set_chunk_acl_tags(f"c_{doc_id}", ["restricted"])
    chunk = store.get_chunk(f"c_{doc_id}")
    assert chunk["acl_tags"] == ["restricted"]


def test_fts5_startup_probe_passes_on_healthy_db(store: SQLiteStore) -> None:
    """健康 DB 上 init_schema 不抛。"""
    # init_schema 已经在 fixture 里跑过；再跑一次确认幂等
    store.init_schema()


def test_fts5_startup_probe_fails_when_corrupted(tmp_path: Path) -> None:
    """chunks_fts 损坏时 init_schema 抛 IndexCorruptedError。"""
    s = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    s.init_schema()  # 健康建库

    # 通过让 _connect 探活时抛 OperationalError 模拟 FTS5 损坏
    real_connect = SQLiteStore._connect

    class _BrokenConnect:
        def __init__(self, conn):
            self._c = conn
            self._probed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._c.close()
            return False

        def execute(self, sql, *args, **kwargs):
            if "FROM chunks_fts" in sql and not self._probed:
                self._probed = True
                raise sqlite3.OperationalError("database disk image is malformed")
            return self._c.execute(sql, *args, **kwargs)

        def executescript(self, sql):
            return self._c.executescript(sql)

        def commit(self):
            self._c.commit()

    def fake_connect(self):
        return _BrokenConnect(real_connect(self))

    with patch.object(SQLiteStore, "_connect", fake_connect):
        with pytest.raises(IndexCorruptedError):
            s.init_schema()
