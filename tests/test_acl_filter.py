from pathlib import Path

import pytest

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


def test_empty_scopes_only_returns_public_documents(store: SQLiteStore) -> None:
    public_doc = _publish(store, "Public SOP", {"network_type": "5g"}, [])
    _publish(store, "Wireless SOP", {"network_type": "5g"}, ["wireless"])
    assert store.list_published_doc_ids_in_scope([]) == [public_doc]


def test_scope_filters_by_acl_tags(store: SQLiteStore) -> None:
    wireless_doc = _publish(store, "Wireless SOP", {"network_type": "5g"}, ["wireless"])
    _publish(store, "Transport SOP", {"network_type": "transport"}, ["transport"])
    assert store.list_published_doc_ids_in_scope(["wireless"]) == [wireless_doc]


def test_scope_filters_by_metadata_value(store: SQLiteStore) -> None:
    five_g_doc = _publish(store, "5G SOP", {"network_type": "5g"}, ["wireless"])
    _publish(store, "4G SOP", {"network_type": "4g"}, ["wireless"])
    assert store.list_published_doc_ids_in_scope(["5g"]) == [five_g_doc]


def test_non_published_excluded(store: SQLiteStore) -> None:
    doc_id = store.create_document(
        "Unpublished SOP", "/tmp/x", "text/plain", {"network_type": "5g"}, ["wireless"], "v1"
    )
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": "c", "chunk_no": 1, "content": "x", "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    assert store.list_published_doc_ids_in_scope(["wireless"]) == []
