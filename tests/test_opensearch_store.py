"""
Tests for OpenSearchSparseStore and StubSparseStore.
Uses a mocked opensearchpy client so no real OpenSearch instance is required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_employee.common_schemas.sparse_store import (
    OpenSearchSparseStore,
    StubSparseStore,
)


# ---------------------------------------------------------------------------
# StubSparseStore tests
# ---------------------------------------------------------------------------

class TestStubSparseStore:
    """In-memory keyword-match store for testing and MVP development."""

    def test_create_index_noop(self) -> None:
        store = StubSparseStore()
        store.create_index("test_index")

    def test_bulk_index_and_search(self) -> None:
        store = StubSparseStore()
        docs = [
            {
                "chunk_id": "c1",
                "content": "5G base station troubleshooting guide",
                "section_path": "/troubleshooting",
                "doc_id": "doc_001",
            },
            {
                "chunk_id": "c2",
                "content": "LTE cell handover parameters",
                "section_path": "/handover",
                "doc_id": "doc_002",
            },
            {
                "chunk_id": "c3",
                "content": "5G massive MIMO antenna configuration",
                "section_path": "/mimo",
                "doc_id": "doc_001",
            },
        ]
        store.bulk_index("kb", docs)

        results = store.search("kb", "5G troubleshooting", top_k=20)
        assert len(results) > 0
        # c1 should be top: has both "5G" and "troubleshooting"
        assert results[0]["chunk_id"] == "c1"

    def test_search_no_match(self) -> None:
        store = StubSparseStore()
        store.bulk_index(
            "kb",
            [{"chunk_id": "c1", "content": "hello world", "section_path": "/", "doc_id": "d1"}],
        )
        results = store.search("kb", "xyzzy notfound")
        assert results == []

    def test_search_filter_by_doc_ids(self) -> None:
        store = StubSparseStore()
        docs = [
            {"chunk_id": "c1", "content": "5G troubleshooting", "section_path": "/a", "doc_id": "doc_001"},
            {"chunk_id": "c2", "content": "5G optimization", "section_path": "/b", "doc_id": "doc_002"},
        ]
        store.bulk_index("kb", docs)

        results = store.search("kb", "5G", doc_ids_filter=["doc_001"], top_k=20)
        assert len(results) == 1
        assert results[0]["chunk_id"] == "c1"

    def test_search_doc_ids_filter_none_includes_all(self) -> None:
        store = StubSparseStore()
        docs = [
            {"chunk_id": "c1", "content": "5G troubleshooting", "section_path": "/a", "doc_id": "doc_001"},
            {"chunk_id": "c2", "content": "5G optimization", "section_path": "/b", "doc_id": "doc_002"},
        ]
        store.bulk_index("kb", docs)

        results = store.search("kb", "5G", doc_ids_filter=None, top_k=20)
        assert len(results) == 2

    def test_search_top_k_truncates(self) -> None:
        store = StubSparseStore()
        docs = [
            {"chunk_id": f"c{i}", "content": f"keyword {i}", "section_path": "/", "doc_id": "d1"}
            for i in range(10)
        ]
        store.bulk_index("kb", docs)

        results = store.search("kb", "keyword", top_k=3)
        assert len(results) == 3

    def test_search_case_insensitive(self) -> None:
        store = StubSparseStore()
        store.bulk_index(
            "kb",
            [{"chunk_id": "c1", "content": "UPPERCASE TERM", "section_path": "/", "doc_id": "d1"}],
        )
        results = store.search("kb", "uppercase term")
        assert len(results) == 1
        assert results[0]["chunk_id"] == "c1"


# ---------------------------------------------------------------------------
# OpenSearchSparseStore tests (mocked opensearchpy)
# ---------------------------------------------------------------------------

class TestOpenSearchSparseStore:
    """Tests using a mocked opensearchpy.OpenSearch client."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def store(self, mock_client: MagicMock) -> OpenSearchSparseStore:
        store = OpenSearchSparseStore()
        store._client = mock_client
        return store

    def test_create_index(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.indices.exists.return_value = False

        store.create_index("test_index")

        mock_client.indices.create.assert_called_once()
        call_kwargs = mock_client.indices.create.call_args.kwargs
        assert call_kwargs["index"] == "test_index"

    def test_create_index_already_exists(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.indices.exists.return_value = True
        store.create_index("test_index")
        mock_client.indices.create.assert_not_called()

    def test_bulk_index(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.bulk.return_value = {"errors": False}

        docs = [
            {"chunk_id": "c1", "content": "5G troubleshooting", "section_path": "/a", "doc_id": "d1"},
            {"chunk_id": "c2", "content": "LTE handover", "section_path": "/b", "doc_id": "d2"},
        ]
        store.bulk_index("kb", docs)

        mock_client.bulk.assert_called_once()
        call_body = mock_client.bulk.call_args.kwargs["body"]
        # body is flat alternating metadata + doc lines
        assert "c1" in str(call_body)
        assert "c2" in str(call_body)

    def test_search(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": "c1",
                        "_score": 2.5,
                        "_source": {
                            "chunk_id": "c1",
                            "doc_id": "d1",
                            "content": "5G troubleshooting guide",
                            "section_path": "/troubleshooting",
                        },
                    }
                ],
            }
        }

        results = store.search("kb", "5G troubleshooting", top_k=20)
        assert len(results) == 1
        assert results[0]["chunk_id"] == "c1"
        assert results[0]["content"] == "5G troubleshooting guide"
        assert results[0]["score"] == 2.5
        assert results[0]["doc_id"] == "d1"
        assert results[0]["section_path"] == "/troubleshooting"

    def test_search_no_results(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        results = store.search("kb", "nothing matches this")
        assert results == []

    def test_search_with_doc_ids_filter(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": "c1",
                        "_score": 1.0,
                        "_source": {"chunk_id": "c1", "doc_id": "d1", "content": "x", "section_path": "/"},
                    }
                ],
            }
        }

        results = store.search("kb", "test", doc_ids_filter=["d1", "d2"])
        assert len(results) == 1

        # Verify the query body includes a terms filter
        call_body = mock_client.search.call_args.kwargs["body"]
        body_str = str(call_body)
        assert "terms" in body_str
        assert "doc_id" in body_str

    def test_search_doc_ids_filter_none(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        store.search("kb", "test", doc_ids_filter=None)
        call_body = mock_client.search.call_args.kwargs["body"]
        body_str = str(call_body)
        # No terms filter should be present when doc_ids_filter is None
        assert "terms" not in body_str

    def test_search_top_k(self, store: OpenSearchSparseStore, mock_client: MagicMock) -> None:
        mock_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        store.search("kb", "test", top_k=5)
        assert mock_client.search.call_args.kwargs["body"]["size"] == 5

    def test_search_connection_error_fallback(self, mock_client: MagicMock) -> None:
        import opensearchpy

        mock_client.search.side_effect = opensearchpy.ConnectionError("refused")

        store = OpenSearchSparseStore()
        store._client = mock_client
        store._fallback = None  # reset fallback

        results = store.search("kb", "test")
        assert results == []

    def test_bulk_index_connection_error_fallback(self, mock_client: MagicMock) -> None:
        import opensearchpy

        mock_client.bulk.side_effect = opensearchpy.ConnectionError("refused")

        store = OpenSearchSparseStore()
        store._client = mock_client
        store._fallback = None

        # Should not raise
        store.bulk_index("kb", [{"chunk_id": "c1", "content": "x", "section_path": "/", "doc_id": "d1"}])
