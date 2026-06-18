"""Document versioning + chunk-level diff tests (spec §4.6)."""
from __future__ import annotations

import pytest

from ai_employee.knowledge_api.versions import (
    DiffResult,
    DocumentVersion,
    VersionStore,
    build_version_store,
    diff_versions,
)


# --------------------------------------------------------------------------- #
# DocumentVersion
# --------------------------------------------------------------------------- #


def test_document_version_to_dict() -> None:
    v = DocumentVersion(
        doc_id="d1",
        version="v1",
        chunks=[
            {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
            {"chunk_id": "c2", "content": "beta", "section_path": "root"},
        ],
        created_at="2026-06-18T00:00:00Z",
    )
    d = v.to_dict()
    assert d["doc_id"] == "d1"
    assert d["version"] == "v1"
    assert len(d["chunks"]) == 2


def test_document_version_chunk_count() -> None:
    v = DocumentVersion(
        doc_id="d1", version="v1", chunks=[
            {"chunk_id": "c1", "content": "a", "section_path": "root"},
            {"chunk_id": "c2", "content": "b", "section_path": "root"},
            {"chunk_id": "c3", "content": "c", "section_path": "root"},
        ],
    )
    assert v.chunk_count == 3


# --------------------------------------------------------------------------- #
# VersionStore
# --------------------------------------------------------------------------- #


def test_store_create_and_list_versions() -> None:
    store = VersionStore()
    store.create(
        doc_id="d1", version="v1",
        chunks=[{"chunk_id": "c1", "content": "alpha", "section_path": "root"}],
    )
    store.create(
        doc_id="d1", version="v2",
        chunks=[
            {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
            {"chunk_id": "c2", "content": "beta", "section_path": "root"},
        ],
    )
    versions = store.list_versions("d1")
    assert [v.version for v in versions] == ["v1", "v2"]


def test_store_get_specific_version() -> None:
    store = VersionStore()
    store.create(doc_id="d1", version="v1", chunks=[])
    store.create(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "x", "section_path": "root"},
    ])
    v2 = store.get("d1", "v2")
    assert v2 is not None
    assert v2.chunk_count == 1


def test_store_get_missing_returns_none() -> None:
    store = VersionStore()
    assert store.get("missing", "v1") is None


def test_store_duplicate_version_rejected() -> None:
    store = VersionStore()
    store.create(doc_id="d1", version="v1", chunks=[])
    with pytest.raises(ValueError):
        store.create(doc_id="d1", version="v1", chunks=[])


def test_store_versions_isolated_per_doc() -> None:
    store = VersionStore()
    store.create(doc_id="d1", version="v1", chunks=[])
    store.create(doc_id="d2", version="v1", chunks=[])
    assert len(store.list_versions("d1")) == 1
    assert len(store.list_versions("d2")) == 1


# --------------------------------------------------------------------------- #
# diff_versions
# --------------------------------------------------------------------------- #


def test_diff_versions_detects_added() -> None:
    a = DocumentVersion(doc_id="d1", version="v1", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
    ])
    b = DocumentVersion(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
        {"chunk_id": "c2", "content": "beta", "section_path": "root"},
    ])
    diff = diff_versions(a, b)
    assert diff.added == ["c2"]
    assert diff.removed == []
    assert diff.modified == []


def test_diff_versions_detects_removed() -> None:
    a = DocumentVersion(doc_id="d1", version="v1", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
        {"chunk_id": "c2", "content": "beta", "section_path": "root"},
    ])
    b = DocumentVersion(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
    ])
    diff = diff_versions(a, b)
    assert diff.removed == ["c2"]
    assert diff.added == []
    assert diff.modified == []


def test_diff_versions_detects_modified() -> None:
    a = DocumentVersion(doc_id="d1", version="v1", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
    ])
    b = DocumentVersion(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "alpha-updated", "section_path": "root"},
    ])
    diff = diff_versions(a, b)
    assert diff.modified == ["c1"]
    assert diff.added == []
    assert diff.removed == []


def test_diff_versions_no_change_is_empty() -> None:
    chunks = [{"chunk_id": "c1", "content": "alpha", "section_path": "root"}]
    a = DocumentVersion(doc_id="d1", version="v1", chunks=chunks)
    b = DocumentVersion(doc_id="d1", version="v2", chunks=chunks)
    diff = diff_versions(a, b)
    assert diff.added == []
    assert diff.removed == []
    assert diff.modified == []


def test_diff_versions_to_dict_serializable() -> None:
    a = DocumentVersion(doc_id="d1", version="v1", chunks=[])
    b = DocumentVersion(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
    ])
    diff = diff_versions(a, b)
    d = diff.to_dict()
    assert d["from_version"] == "v1"
    assert d["to_version"] == "v2"
    assert d["added"] == ["c1"]


def test_diff_versions_ignores_section_path_changes() -> None:
    """A chunk whose content didn't change is not "modified" even if the
    section_path was re-classified."""
    a = DocumentVersion(doc_id="d1", version="v1", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "old/path"},
    ])
    b = DocumentVersion(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "new/path"},
    ])
    diff = diff_versions(a, b)
    assert diff.modified == []


def test_diff_versions_combined_changes() -> None:
    a = DocumentVersion(doc_id="d1", version="v1", chunks=[
        {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
        {"chunk_id": "c2", "content": "beta", "section_path": "root"},
    ])
    b = DocumentVersion(doc_id="d1", version="v2", chunks=[
        {"chunk_id": "c1", "content": "alpha-modified", "section_path": "root"},
        {"chunk_id": "c3", "content": "gamma", "section_path": "root"},
    ])
    diff = diff_versions(a, b)
    assert diff.added == ["c3"]
    assert diff.removed == ["c2"]
    assert diff.modified == ["c1"]


def test_build_version_store_returns_singleton() -> None:
    a = build_version_store()
    b = build_version_store()
    assert a is b