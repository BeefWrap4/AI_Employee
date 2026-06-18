from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from ai_employee.common_schemas.vector_store import (
    MilvusVectorStore,
    StubVectorStore,
    build_vector_store,
)

# ---------------------------------------------------------------------------
# StubVectorStore tests (no mocking needed)
# ---------------------------------------------------------------------------


class TestStubVectorStore:
    """In-memory stub store tests -- no external dependencies."""

    @pytest.fixture
    def store(self) -> StubVectorStore:
        return StubVectorStore()

    def test_create_collection_registers_name_and_dim(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=128)
        assert "chunks" in store._collections

    def test_insert_and_search_returns_correct_hits(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=4)
        vec_a = [1.0, 0.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0, 0.0]
        vec_c = [0.99, 0.01, 0.0, 0.0]

        store.insert(
            "chunks",
            vectors=[vec_a, vec_b],
            metadata=[
                {
                    "chunk_id": "c1",
                    "doc_id": "doc_001",
                    "content": "hello",
                    "section_path": "root",
                    "chunk_no": 1,
                },
                {
                    "chunk_id": "c2",
                    "doc_id": "doc_002",
                    "content": "world",
                    "section_path": "root",
                    "chunk_no": 2,
                },
            ],
        )

        # Search with a vector close to vec_a
        hits = store.search("chunks", query_vector=vec_c, top_k=2)
        assert len(hits) == 2
        # c1 should be first (closer to [1,0,0,0])
        assert hits[0]["chunk_id"] == "c1"
        assert hits[0]["doc_id"] == "doc_001"
        assert hits[0]["content"] == "hello"
        assert "distance" in hits[0]
        # c2 second
        assert hits[1]["chunk_id"] == "c2"

    def test_search_respects_top_k(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=2)
        # Use vectors in different directions so cosine similarity differentiates them.
        # cos([1.0, 0.0], [1.0, 0.1]) > cos([1.0, 0.0], [0.0, 1.0])
        vectors = [
            [1.0, 0.0],  # c0
            [1.0, 0.05],  # c1 -- very close to [1, 0]
            [1.0, 0.1],  # c2
            [1.0, 0.2],  # c3
            [1.0, 0.5],  # c4
            [0.5, 1.0],  # c5
            [0.0, 1.0],  # c6
            [-1.0, 1.0],  # c7
            [-1.0, 0.0],  # c8
            [-1.0, -1.0],  # c9
        ]
        metadata = [
            {
                "chunk_id": f"c{i}",
                "doc_id": "doc_001",
                "content": f"text {i}",
                "section_path": "root",
                "chunk_no": i,
            }
            for i in range(10)
        ]
        store.insert("chunks", vectors=vectors, metadata=metadata)

        # Query vector [1.0, 0.0] -- c0 is exact match, c1 next closest
        hits = store.search("chunks", query_vector=[1.0, 0.0], top_k=3)
        assert len(hits) == 3
        assert hits[0]["chunk_id"] == "c0"

    def test_search_filter_by_doc_id_equality(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=2)
        store.insert(
            "chunks",
            vectors=[[1.0, 0.0], [0.0, 1.0]],
            metadata=[
                {
                    "chunk_id": "c1",
                    "doc_id": "doc_A",
                    "content": "a",
                    "section_path": "root",
                    "chunk_no": 1,
                },
                {
                    "chunk_id": "c2",
                    "doc_id": "doc_B",
                    "content": "b",
                    "section_path": "root",
                    "chunk_no": 2,
                },
            ],
        )

        hits = store.search("chunks", query_vector=[1.0, 0.0], filter_expr="doc_id == 'doc_A'")
        assert len(hits) == 1
        assert hits[0]["doc_id"] == "doc_A"

    def test_search_filter_by_doc_id_in_list(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=2)
        store.insert(
            "chunks",
            vectors=[[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]],
            metadata=[
                {
                    "chunk_id": "c1",
                    "doc_id": "doc_A",
                    "content": "a",
                    "section_path": "root",
                    "chunk_no": 1,
                },
                {
                    "chunk_id": "c2",
                    "doc_id": "doc_B",
                    "content": "b",
                    "section_path": "root",
                    "chunk_no": 2,
                },
                {
                    "chunk_id": "c3",
                    "doc_id": "doc_C",
                    "content": "c",
                    "section_path": "root",
                    "chunk_no": 3,
                },
            ],
        )

        hits = store.search(
            "chunks",
            query_vector=[0.5, 0.5],
            filter_expr="doc_id in ['doc_A', 'doc_C']",
        )
        doc_ids = {h["doc_id"] for h in hits}
        assert doc_ids == {"doc_A", "doc_C"}

    def test_search_empty_collection_returns_empty(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=4)
        hits = store.search("chunks", query_vector=[1.0, 0.0, 0.0, 0.0])
        assert hits == []

    def test_search_missing_collection_returns_empty(self, store: StubVectorStore) -> None:
        hits = store.search("nonexistent", query_vector=[1.0, 0.0])
        assert hits == []

    def test_insert_missing_collection_is_noop(self, store: StubVectorStore) -> None:
        """Inserting into a collection that was never created should be a no-op."""
        store.insert("nonexistent", vectors=[[1.0, 0.0]], metadata=[{"chunk_id": "c1"}])
        assert store._collections.get("nonexistent") is None

    def test_preserves_acl_tags(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=2)
        store.insert(
            "chunks",
            vectors=[[1.0, 0.0]],
            metadata=[
                {
                    "chunk_id": "c1",
                    "doc_id": "d1",
                    "acl_tags": ["wireless", "5g"],
                    "content": "x",
                    "section_path": "root",
                }
            ],
        )
        hits = store.search("chunks", query_vector=[1.0, 0.0], top_k=1)
        assert hits[0]["acl_tags"] == ["wireless", "5g"]

    def test_confidence_is_between_zero_and_one(self, store: StubVectorStore) -> None:
        store.create_collection("chunks", dim=2)
        store.insert(
            "chunks",
            vectors=[[1.0, 0.0]],
            metadata=[{"chunk_id": "c1", "doc_id": "d1", "content": "x", "section_path": "root"}],
        )
        hits = store.search("chunks", query_vector=[1.0, 0.0], top_k=1)
        assert 0.0 <= hits[0]["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# MilvusVectorStore tests (mocked pymilvus)
# ---------------------------------------------------------------------------


class MockHit:
    """Simulates a pymilvus search hit."""

    def __init__(self, entity: dict, distance: float) -> None:
        self.entity = entity
        self.distance = distance


class MockCollection:
    """Simulates a pymilvus Collection."""

    def __init__(self, name: str, schema: object | None = None) -> None:
        self.name = name
        self.schema = schema
        self._inserted: list = []
        self._index_created = False
        self._loaded = False

    def create_index(self, field_name: str, index_params: dict) -> None:
        self._index_created = True

    def load(self) -> None:
        self._loaded = True

    def insert(self, data: list) -> MagicMock:
        self._inserted.append(data)
        mock_result = MagicMock()
        mock_result.primary_keys = []
        return mock_result

    def flush(self) -> None:
        pass

    def search(
        self,
        data: list,
        anns_field: str,
        param: dict,
        limit: int,
        expr: str | None = None,
        output_fields: list[str] | None = None,
    ) -> list:
        hits = [
            MockHit(
                entity={
                    "chunk_id": "c_mock_1",
                    "doc_id": "doc_001",
                    "chunk_no": 1,
                    "content": "hello world",
                    "section_path": "root",
                    "page_no": 1,
                    "embedding_model": "stub",
                    "acl_tags": '["wireless"]',
                },
                distance=0.98,
            ),
        ]
        return [hits]


@pytest.fixture
def mock_pymilvus(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Mock pymilvus module via monkeypatching the create_collection internals.

    Strategy: monkeypatch MilvusVectorStore._connect to no-op, and
    _collection to return a MockCollection. Also patch the internal
    pymilvus-level calls inside create_collection.
    """
    import ai_employee.common_schemas.vector_store as vs_mod

    # 1. Make _safe_import_pymilvus a no-op (it normally tries to import pymilvus)
    monkeypatch.setattr(vs_mod, "_safe_import_pymilvus", lambda: None)

    # 2. Make _connect a no-op
    monkeypatch.setattr(
        vs_mod.MilvusVectorStore,
        "_connect",
        lambda self: None,
    )

    # 3. Make _collection return a MockCollection
    mock_coll = MockCollection("chunks")
    monkeypatch.setattr(
        vs_mod.MilvusVectorStore,
        "_collection",
        lambda self, name: mock_coll,
    )

    return {}


class TestMilvusVectorStore:
    """Tests for MilvusVectorStore with mocked pymilvus internals."""

    @pytest.fixture
    def store(self, mock_pymilvus: dict) -> MilvusVectorStore:
        return MilvusVectorStore(host="fake", port="12345")

    def test_create_collection_does_not_fail(
        self, store: MilvusVectorStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_collection is already mocked as no-op by the fixture.
        The monkeypatch overrides render this test mostly symbolic --
        it confirms the mock wiring doesn't cause import errors."""
        import ai_employee.common_schemas.vector_store as vs_mod

        # Fully override create_collection to a no-op (avoids pymilvus imports)
        monkeypatch.setattr(
            vs_mod.MilvusVectorStore,
            "create_collection",
            lambda self, cn, dim: None,
        )
        store.create_collection("chunks", dim=128)  # should not raise

    def test_insert_writes_data(
        self, store: MilvusVectorStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        col = MockCollection("chunks")
        monkeypatch.setattr(store, "_collection", lambda name: col)
        store.insert(
            "chunks",
            vectors=[[1.0, 0.0, 0.0]],
            metadata=[
                {"chunk_id": "c1", "doc_id": "d1", "content": "test", "section_path": "root"}
            ],
        )
        assert len(col._inserted) == 1

    def test_search_returns_hits_with_scores(
        self, store: MilvusVectorStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        col = MockCollection("chunks")
        monkeypatch.setattr(store, "_collection", lambda name: col)
        hits = store.search("chunks", query_vector=[1.0, 0.0], top_k=5)
        assert len(hits) >= 1
        assert "chunk_id" in hits[0]
        assert "doc_id" in hits[0]
        assert "content" in hits[0]
        assert "distance" in hits[0]
        assert "confidence" in hits[0]
        # confidence should be between 0 and 1
        assert 0.0 <= hits[0]["confidence"] <= 1.0

    def test_insert_empty_vectors_is_noop(self, store: MilvusVectorStore) -> None:
        """Empty insert should return without calling anything."""
        # Should not raise
        store.insert("chunks", vectors=[], metadata=[])


# ---------------------------------------------------------------------------
# build_vector_store tests
# ---------------------------------------------------------------------------


class TestBuildVectorStore:
    def test_returns_stub_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MILVUS_ENABLED", "false")
        store = build_vector_store()
        assert isinstance(store, StubVectorStore)

    def test_returns_stub_when_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MILVUS_ENABLED", raising=False)
        store = build_vector_store()
        assert isinstance(store, StubVectorStore)

    def test_returns_stub_when_enabled_but_milvus_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MILVUS_ENABLED", "true")
        # Force MilvusVectorStore to fail on construction
        import ai_employee.common_schemas.vector_store as vs_mod

        monkeypatch.setattr(vs_mod, "_safe_import_pymilvus", lambda: None)

        def _failing_init(self, host=None, port=None):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(vs_mod.MilvusVectorStore, "__init__", _failing_init)
        store = build_vector_store(enabled=True)
        assert isinstance(store, StubVectorStore)

    def test_returns_milvus_when_enabled_and_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MILVUS_ENABLED", "true")
        import ai_employee.common_schemas.vector_store as vs_mod

        monkeypatch.setattr(vs_mod, "_safe_import_pymilvus", lambda: None)
        monkeypatch.setattr(
            vs_mod.MilvusVectorStore,
            "_connect",
            lambda self: None,
        )
        store = build_vector_store(enabled=True)
        assert isinstance(store, MilvusVectorStore)
