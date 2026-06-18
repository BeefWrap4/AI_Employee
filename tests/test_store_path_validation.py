from pathlib import Path

import pytest
from ai_employee.knowledge_api.store import SQLiteStore
from fastapi import HTTPException


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    raw = tmp_path / "raw"
    raw.mkdir()
    s = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    s.init_schema()
    return s


def test_set_source_uri_accepts_path_inside_raw(store: SQLiteStore, tmp_path: Path) -> None:
    p = tmp_path / "raw" / "x.md"
    p.write_text("x", encoding="utf-8")
    doc_id = store.create_document("S", str(p), "text/plain", {}, [], "v1")
    store.set_source_uri(doc_id, str(p))  # 不抛
    assert store.get_document(doc_id)["source_uri"] == str(p.resolve())


def test_set_source_uri_rejects_relative_path(store: SQLiteStore) -> None:
    doc_id = store.create_document("S", "/tmp/x", "text/plain", {}, [], "v1")
    with pytest.raises(HTTPException) as exc:
        store.set_source_uri(doc_id, "x.md")
    assert exc.value.status_code == 500
    assert exc.value.detail["error_code"] == "path_not_allowed"


def test_set_source_uri_rejects_path_outside_raw(store: SQLiteStore, tmp_path: Path) -> None:
    p = tmp_path / "outside.md"
    p.write_text("x", encoding="utf-8")
    doc_id = store.create_document("S", "/tmp/x", "text/plain", {}, [], "v1")
    with pytest.raises(HTTPException) as exc:
        store.set_source_uri(doc_id, str(p))
    assert exc.value.detail["error_code"] == "path_not_allowed"


def test_set_source_uri_rejects_parent_traversal(store: SQLiteStore, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    p = raw / "x.md"
    p.write_text("x", encoding="utf-8")
    evil = raw / ".." / "raw_sub" / "y.md"
    doc_id = store.create_document("S", str(p), "text/plain", {}, [], "v1")
    with pytest.raises(HTTPException):
        store.set_source_uri(doc_id, str(evil))


def test_set_source_uri_does_not_mutate_db_on_reject(store: SQLiteStore, tmp_path: Path) -> None:
    """校验失败不应写入 DB（事务回滚）。"""
    p = tmp_path / "raw" / "x.md"
    p.write_text("x", encoding="utf-8")
    doc_id = store.create_document("S", str(p), "text/plain", {}, [], "v1")
    # 初始 source_uri 是创建时的路径
    before = store.get_document(doc_id)["source_uri"]
    # 尝试用非法路径
    with pytest.raises(HTTPException):
        store.set_source_uri(doc_id, "/etc/passwd")
    after = store.get_document(doc_id)["source_uri"]
    assert after == before  # 未被污染
