from pathlib import Path

import pytest

from ai_employee.common_schemas.acl import resolve_visible_docs
from ai_employee.knowledge_api.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    s.init_schema()
    return s


def _publish(store: SQLiteStore, title: str, metadata: dict, acl_tags: list[str]) -> str:
    doc_id = store.create_document(title, "/tmp/x", "text/plain", metadata, acl_tags, "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": title, "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")
    return doc_id


def test_only_scope_hits(store: SQLiteStore) -> None:
    d1 = _publish(store, "无线 SOP", {"network_type": "5g"}, ["wireless"])
    _publish(store, "传输 SOP", {"network_type": "transport"}, ["transport"])
    assert set(resolve_visible_docs(store, ["wireless"], None)) == {d1}


def test_only_scope_or_hits(store: SQLiteStore) -> None:
    d1 = _publish(store, "无线 5G", {"network_type": "5g"}, ["wireless"])
    d2 = _publish(store, "无线 4G", {"network_type": "4g"}, ["wireless"])
    _publish(store, "传输", {"network_type": "transport"}, ["transport"])
    # scope_or=[5g]：metadata.network_type==5g 命中
    assert set(resolve_visible_docs(store, None, ["5g"])) == {d1}
    # 切换 scope_or=[wireless]：acl_tags 命中
    assert set(resolve_visible_docs(store, None, ["wireless"])) == {d1, d2}


def test_scope_AND_scope_or_union(store: SQLiteStore) -> None:
    d_wireless = _publish(store, "无线", {"network_type": "5g"}, ["wireless"])
    d_5g = _publish(store, "5G", {"network_type": "5g"}, ["noc"])
    _publish(store, "传输", {"network_type": "transport"}, ["transport"])
    # scope=[wireless] OR scope_or=[5g]：union 是 {wireless, 5g}
    # 无线 doc 命中 acl_tags=wireless；5G doc 命中 metadata=5g；传输不命中
    assert set(resolve_visible_docs(store, ["wireless"], ["5g"])) == {d_wireless, d_5g}


def test_empty_scope_and_scope_or_returns_all_published(store: SQLiteStore) -> None:
    d1 = _publish(store, "无线", {"network_type": "5g"}, ["wireless"])
    d2 = _publish(store, "传输", {"network_type": "transport"}, ["transport"])
    assert set(resolve_visible_docs(store, None, None)) == {d1, d2}
    assert set(resolve_visible_docs(store, [], [])) == {d1, d2}


def test_empty_acl_doc_not_visible_when_scope_set(store: SQLiteStore) -> None:
    """acl_tags=[] 且 metadata 不覆盖 scope → 不命中。"""
    d_empty = _publish(store, "无 ACL", {"network_type": "5g"}, [])
    _publish(store, "无线", {"network_type": "5g"}, ["wireless"])
    assert d_empty not in resolve_visible_docs(store, ["wireless"], None)
    assert d_empty not in resolve_visible_docs(store, None, ["wireless"])


def test_returns_sorted_by_doc_id(store: SQLiteStore) -> None:
    d1 = _publish(store, "A", {"network_type": "5g"}, ["wireless"])
    d2 = _publish(store, "B", {"network_type": "5g"}, ["wireless"])
    d3 = _publish(store, "C", {"network_type": "5g"}, ["wireless"])
    result = resolve_visible_docs(store, ["wireless"], None)
    assert result == sorted([d1, d2, d3])


def test_excludes_unpublished_documents(store: SQLiteStore) -> None:
    doc_id = store.create_document("未发布", "/tmp/x", "text/plain", {"network_type": "5g"}, ["wireless"], "v1")
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": "c", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    # 不发布
    assert doc_id not in resolve_visible_docs(store, ["wireless"], None)
    assert doc_id not in resolve_visible_docs(store, None, None)
