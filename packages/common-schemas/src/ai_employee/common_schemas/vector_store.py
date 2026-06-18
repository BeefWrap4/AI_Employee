"""Vector store abstraction for Milvus and in-memory (stub) storage.

Provides pluggable vector storage for ingestion and retrieval. Milvus is an
ADDITIONAL store layered alongside the existing SQLite path -- not a replacement.

When MILVUS_ENABLED is false (default for MVP dev), the stub store is used
and all operations are no-ops or in-memory.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class VectorStore(Protocol):
    """Abstract interface for vector storage backends."""

    def create_collection(self, collection_name: str, dim: int) -> None:
        """Ensure the collection exists with the given vector dimension."""
        ...

    def insert(
        self,
        collection_name: str,
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> None:
        """Insert vectors with associated metadata into the collection."""
        ...

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 20,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for top_k nearest vectors, optionally filtered.

        Returns list of dicts with at least: id, distance, plus metadata fields.
        """
        ...


# ---------------------------------------------------------------------------
# Milvus
# ---------------------------------------------------------------------------


def _safe_import_pymilvus() -> None:
    """Import pymilvus; raise a clear error if not installed."""
    try:
        import pymilvus  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "pymilvus is required for MilvusVectorStore. " "Install it with: pip install pymilvus"
        ) from exc


class MilvusVectorStore:
    """Milvus-backed vector store using pymilvus.

    Connection settings are read from environment variables:
      - MILVUS_HOST (default: localhost)
      - MILVUS_PORT (default: 19530)

    Collections use a simple schema:
      - id: int64 primary key (auto_id)
      - chunk_id: VarChar
      - doc_id: VarChar
      - chunk_no: int64
      - content: VarChar
      - section_path: VarChar
      - page_no: int64
      - embedding: FloatVector(dim)
      - embedding_model: VarChar
      - acl_tags: VarChar (JSON string)
    """

    def __init__(
        self,
        host: str | None = None,
        port: str | int | None = None,
    ) -> None:
        _safe_import_pymilvus()
        self.host = host or os.getenv("MILVUS_HOST", "localhost")
        self.port = str(port or os.getenv("MILVUS_PORT", "19530"))
        self._alias = "default"
        self._collections: dict[str, Any] = {}
        self._connected = False

    def _connect(self) -> None:
        if self._connected:
            return
        from pymilvus import connections

        connections.connect(
            alias=self._alias,
            host=self.host,
            port=self.port,
        )
        self._connected = True

    def _collection(self, collection_name: str) -> Any:
        from pymilvus import Collection

        if collection_name not in self._collections:
            self._connect()
            self._collections[collection_name] = Collection(collection_name)
        return self._collections[collection_name]

    # ------------------------------------------------------------------
    # VectorStore interface
    # ------------------------------------------------------------------

    def create_collection(self, collection_name: str, dim: int) -> None:
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, utility

        self._connect()
        if utility.has_collection(collection_name):
            return  # already exists, reuse

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="chunk_no", dtype=DataType.INT64),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="section_path", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="page_no", dtype=DataType.INT64),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="embedding_model", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="acl_tags", dtype=DataType.VARCHAR, max_length=4096),
        ]
        schema = CollectionSchema(fields, description=f"AI Employee {collection_name}")
        col = Collection(name=collection_name, schema=schema)
        self._collections[collection_name] = col

        # Create index for the embedding field
        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        col.create_index(field_name="embedding", index_params=index_params)
        col.load()

    def insert(
        self,
        collection_name: str,
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> None:
        if not vectors:
            return
        col = self._collection(collection_name)
        # Build the insert data in columnar format
        rows: list[list[Any]] = [[], [], [], [], [], [], [], [], []]
        for meta, vec in zip(metadata, vectors, strict=False):
            rows[0].append(meta.get("chunk_id", ""))
            rows[1].append(meta.get("doc_id", ""))
            rows[2].append(meta.get("chunk_no", 0))
            rows[3].append(meta.get("content", ""))
            rows[4].append(meta.get("section_path", "root"))
            rows[5].append(meta.get("page_no", 1))
            rows[6].append(vec)
            rows[7].append(meta.get("embedding_model", ""))
            rows[8].append(json.dumps(meta.get("acl_tags", []), ensure_ascii=False))
        col.insert(rows)
        col.flush()

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 20,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        col = self._collection(collection_name)
        col.load()
        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        results = col.search(
            data=[query_vector],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            expr=filter_expr,
            output_fields=[
                "chunk_id",
                "doc_id",
                "chunk_no",
                "content",
                "section_path",
                "page_no",
                "embedding_model",
                "acl_tags",
            ],
        )
        hits: list[dict[str, Any]] = []
        for hit in results[0]:
            record: dict[str, Any] = {
                "chunk_id": hit.entity.get("chunk_id"),
                "doc_id": hit.entity.get("doc_id"),
                "chunk_no": hit.entity.get("chunk_no"),
                "content": hit.entity.get("content"),
                "section_path": hit.entity.get("section_path"),
                "page_no": hit.entity.get("page_no"),
                "embedding_model": hit.entity.get("embedding_model"),
                "distance": hit.distance,
                "confidence": max(0.0, min(1.0, (hit.distance + 1.0) / 2.0)),
            }
            acl_raw = hit.entity.get("acl_tags")
            record["acl_tags"] = json.loads(acl_raw) if acl_raw else []
            hits.append(record)
        return hits


# ---------------------------------------------------------------------------
# Stub (in-memory) vector store
# ---------------------------------------------------------------------------


class StubVectorStore:
    """In-memory fallback store that implements the same interface as Milvus.

    Useful for testing, CI, and local development without Milvus infrastructure.
    Supports a basic filter_expr subset: doc_id == '...' and doc_id in [...].
    """

    def __init__(self) -> None:
        self._collections: dict[str, dict] = {}
        # Each collection: {"dim": int, "rows": list[dict]}
        # Row fields: chunk_id, doc_id, chunk_no, content, section_path,
        #   page_no, embedding, embedding_model, acl_tags

    def create_collection(self, collection_name: str, dim: int) -> None:
        if collection_name not in self._collections:
            self._collections[collection_name] = {"dim": dim, "rows": []}

    def insert(
        self,
        collection_name: str,
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> None:
        if collection_name not in self._collections:
            return
        coll = self._collections[collection_name]
        for meta, vec in zip(metadata, vectors, strict=False):
            coll["rows"].append(
                {
                    "chunk_id": meta.get("chunk_id", ""),
                    "doc_id": meta.get("doc_id", ""),
                    "chunk_no": meta.get("chunk_no", 0),
                    "content": meta.get("content", ""),
                    "section_path": meta.get("section_path", "root"),
                    "page_no": meta.get("page_no", 1),
                    "embedding": vec,
                    "embedding_model": meta.get("embedding_model", ""),
                    "acl_tags": meta.get("acl_tags", []),
                }
            )

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 20,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        if collection_name not in self._collections:
            return []
        rows = self._collections[collection_name]["rows"]

        # Apply filter
        filtered = self._apply_filter(rows, filter_expr)

        # Compute cosine similarity and sort
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in filtered:
            sim = _cosine(query_vector, row["embedding"])
            scored.append((sim, row))
        scored.sort(key=lambda x: x[0], reverse=True)

        hits: list[dict[str, Any]] = []
        for sim, row in scored[:top_k]:
            hits.append(
                {
                    **{k: v for k, v in row.items() if k != "embedding"},
                    "distance": sim,
                    "confidence": max(0.0, min(1.0, (sim + 1.0) / 2.0)),
                }
            )
        return hits

    @staticmethod
    def _apply_filter(rows: list[dict[str, Any]], filter_expr: str | None) -> list[dict[str, Any]]:
        if not filter_expr:
            return list(rows)

        # Support: doc_id == '...' and doc_id in [...]
        import re

        # Parse doc_id == '...'
        eq_match = re.match(r"""doc_id\s*==\s*'([^']+)'""", filter_expr)
        if eq_match:
            target = eq_match.group(1)
            return [r for r in rows if r.get("doc_id") == target]

        # Parse doc_id in [...] with single-quoted strings
        in_match = re.match(r"""doc_id\s+in\s+\[([^\]]+)\]""", filter_expr)
        if in_match:
            inner = in_match.group(1)
            values = re.findall(r"'([^']+)'", inner)
            if values:
                return [r for r in rows if r.get("doc_id") in set(values)]

        # Unknown filter -- return all rows (graceful degradation)
        return list(rows)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_vector_store(
    enabled: bool | None = None,
) -> VectorStore:
    """Build the appropriate vector store based on MILVUS_ENABLED env var.

    Returns MilvusVectorStore when enabled and reachable, StubVectorStore otherwise.
    """
    if enabled is None:
        enabled = os.getenv("MILVUS_ENABLED", "false").lower() in ("1", "true", "yes")
    if not enabled:
        logger.info("Vector store: Milvus disabled, using stub store")
        return StubVectorStore()

    try:
        store = MilvusVectorStore()
        # Quick connection check
        store._connect()
        logger.info("Vector store: Milvus connected at %s:%s", store.host, store.port)
        return store
    except Exception as exc:
        logger.warning("Vector store: Milvus unavailable (%s), using stub store", exc)
        return StubVectorStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = [
    "VectorStore",
    "MilvusVectorStore",
    "StubVectorStore",
    "build_vector_store",
]
