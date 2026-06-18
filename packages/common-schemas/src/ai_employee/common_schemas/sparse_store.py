"""
Sparse (full-text / BM25) vector store abstraction.

Provides two implementations:

- **OpenSearchSparseStore**: Uses opensearchpy to talk to OpenSearch for
  BM25-based full-text search. Falls back to StubSparseStore on connection
  errors.
- **StubSparseStore**: In-memory keyword-match for testing and MVP development
  when OpenSearch is not available.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StubSparseStore -- in-memory keyword match
# ---------------------------------------------------------------------------


class StubSparseStore:
    """In-memory sparse store using simple keyword intersection scoring.

    Tokens are extracted by splitting on non-alphanumeric characters
    (case-insensitive). Scoring: number of query tokens found in the document
    text. Documents with zero matching tokens are excluded.
    """

    def __init__(self) -> None:
        self._indices: dict[str, list[dict[str, Any]]] = {}

    def create_index(self, index_name: str) -> None:
        if index_name not in self._indices:
            self._indices[index_name] = []

    def bulk_index(self, index_name: str, documents: list[dict[str, Any]]) -> None:
        if index_name not in self._indices:
            self._indices[index_name] = []
        self._indices[index_name].extend(documents)

    def search(
        self,
        index_name: str,
        query: str,
        doc_ids_filter: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        docs = self._indices.get(index_name, [])
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        scored: list[tuple[int, dict[str, Any]]] = []
        for d in docs:
            if doc_ids_filter is not None and d.get("doc_id") not in doc_ids_filter:
                continue
            doc_tokens = _tokenize(d.get("content", ""))
            hits = len(query_tokens & doc_tokens)
            if hits > 0:
                scored.append((hits, d))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, doc in scored[:top_k]:
            results.append(dict(doc))
        return results


# ---------------------------------------------------------------------------
# OpenSearchSparseStore
# ---------------------------------------------------------------------------


class OpenSearchSparseStore:
    """BM25 full-text search backed by OpenSearch.

    Connection parameters come from environment variables:
    - OPENSEARCH_HOST (default: "localhost")
    - OPENSEARCH_PORT (default: 9200)

    Uses the fallback StubSparseStore when connection errors occur.
    """

    # BM25 index mapping: content for full-text, section_path and doc_id as
    # keyword fields for filtering.
    _INDEX_BODY = {
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "similarity": {"default": {"type": "BM25"}},
            }
        },
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "content": {"type": "text", "analyzer": "standard"},
                "section_path": {"type": "keyword"},
            }
        },
    }

    def __init__(self) -> None:
        self._host = os.getenv("OPENSEARCH_HOST", "localhost")
        self._port = int(os.getenv("OPENSEARCH_PORT", "9200"))
        self._client: Any = None
        self._fallback: StubSparseStore | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from opensearchpy import OpenSearch
        except ImportError:
            logger.warning("opensearchpy not installed; using stub sparse store")
            return None
        try:
            self._client = OpenSearch(
                hosts=[{"host": self._host, "port": self._port}],
                http_compress=True,
                timeout=10,
                max_retries=1,
                retry_on_timeout=True,
            )
        except Exception:
            logger.warning("Failed to create OpenSearch client; using stub", exc_info=True)
            self._client = None
        return self._client

    def _get_fallback(self) -> StubSparseStore:
        if self._fallback is None:
            self._fallback = StubSparseStore()
        return self._fallback

    def create_index(self, index_name: str) -> None:
        client = self._get_client()
        if client is None:
            return  # stub has no persistent index to create
        try:
            if not client.indices.exists(index=index_name):
                client.indices.create(index=index_name, body=self._INDEX_BODY)
        except Exception:
            logger.warning("Failed to create OpenSearch index %r", index_name, exc_info=True)

    def bulk_index(self, index_name: str, documents: list[dict[str, Any]]) -> None:
        client = self._get_client()
        if client is None:
            self._get_fallback().bulk_index(index_name, documents)
            return

        # Build flat body for bulk: action metadata + source doc per doc
        body: list[dict[str, Any]] = []
        for doc in documents:
            body.append({"index": {"_index": index_name, "_id": doc["chunk_id"]}})
            body.append(
                {
                    "chunk_id": doc["chunk_id"],
                    "doc_id": doc.get("doc_id", ""),
                    "content": doc.get("content", ""),
                    "section_path": doc.get("section_path", ""),
                }
            )

        try:
            client.bulk(body=body, refresh=True)
        except Exception:
            logger.warning("OpenSearch bulk_index failed; writing to stub", exc_info=True)
            self._get_fallback().bulk_index(index_name, documents)

    def search(
        self,
        index_name: str,
        query: str,
        doc_ids_filter: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        client = self._get_client()
        if client is None:
            return self._get_fallback().search(
                index_name,
                query,
                doc_ids_filter=doc_ids_filter,
                top_k=top_k,
            )

        search_body: dict[str, Any] = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["content", "section_path"],
                                "type": "best_fields",
                            }
                        }
                    ],
                }
            },
        }

        if doc_ids_filter is not None:
            search_body["query"]["bool"]["filter"] = [{"terms": {"doc_id": doc_ids_filter}}]

        try:
            resp = client.search(index=index_name, body=search_body)
        except Exception:
            logger.warning("OpenSearch search failed; falling back to stub", exc_info=True)
            return self._get_fallback().search(
                index_name,
                query,
                doc_ids_filter=doc_ids_filter,
                top_k=top_k,
            )

        results: list[dict[str, Any]] = []
        for hit in resp["hits"]["hits"]:
            source = hit["_source"]
            results.append(
                {
                    "chunk_id": source["chunk_id"],
                    "doc_id": source.get("doc_id", ""),
                    "content": source.get("content", ""),
                    "section_path": source.get("section_path", ""),
                    "score": hit["_score"],
                }
            )
        return results


# ---------------------------------------------------------------------------
# Tokenizer shared by StubSparseStore
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}
