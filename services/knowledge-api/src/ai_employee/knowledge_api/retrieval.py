from __future__ import annotations

import math
import os
from dataclasses import dataclass

from fastapi import HTTPException, status

from ai_employee.common_schemas.acl import resolve_visible_docs
from ai_employee.common_schemas.embedding import (
    EmbeddingProvider,
    EmbeddingUnavailableError,
    StubEmbeddingProvider,
)
from ai_employee.common_schemas.sparse_store import (
    OpenSearchSparseStore,
    StubSparseStore,
)
from ai_employee.common_schemas.vector_store import (
    VectorStore,
    build_vector_store,
)
from ai_employee.knowledge_api.store import SQLiteStore


@dataclass
class RetrievalHit:
    chunk_id: str
    doc_id: str
    doc_title: str
    content: str
    section_path: str
    page_no: int
    confidence: float


class RetrievalService:
    def __init__(
        self,
        store: SQLiteStore,
        query_provider: EmbeddingProvider | None = None,
        top_k: int = 3,
        sparse_store: OpenSearchSparseStore | StubSparseStore | None = None,
        vector_store: VectorStore | None = None,
        query_rewriter: QueryRewriter | None = None,
    ) -> None:
        self.store = store
        self.query_provider = query_provider or StubEmbeddingProvider(dim=8)
        self.top_k = top_k
        self.query_rewriter = query_rewriter
        # Determine sparse store: injected > OPENSEARCH_ENABLED env > Stub fallback
        if sparse_store is not None:
            self.sparse_store: OpenSearchSparseStore | StubSparseStore = sparse_store
        elif os.getenv("OPENSEARCH_ENABLED", "false").strip().lower() == "true":
            _store = OpenSearchSparseStore()
            _store.create_index("knowledge_base")
            self.sparse_store = _store
        else:
            self.sparse_store = StubSparseStore()
        # Determine vector store: injected > build from env > None (uses SQLite fallback)
        if vector_store is not None:
            self.vector_store: VectorStore | None = vector_store
        else:
            self.vector_store = build_vector_store()

    def search(
        self,
        question: str,
        scopes: list[str],
        scope_or: list[str] | None = None,
        top_k: int | None = None,
        query_rewriter: QueryRewriter | None = None,
    ) -> list[RetrievalHit]:
        top_k = top_k or self.top_k
        scope_or = scope_or or []
        # 文档级 ACL：scope AND scope_or 联合（任一与 doc 可见集相交即命中）
        doc_ids = resolve_visible_docs(self.store, scopes, scope_or)
        if not doc_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "no_knowledge_in_scope"},
            )

        effective = set(scopes or []) | set(scope_or or [])

        # BM25 full-text recall: prefer OpenSearch if enabled and data is there.
        # Fall back to SQLite FTS5 when OpenSearch is not enabled or returns nothing.
        bm25_rows: list[dict] = []
        os_results = self.sparse_store.search(
            "knowledge_base", question, doc_ids_filter=doc_ids, top_k=20,
        )
        if os_results:
            # OpenSearch returned results -- use them as BM25 recall.
            # Enrich with SQLite chunk metadata (acl_tags, embedding, title).
            for r in os_results:
                chunk = self.store.get_chunk(r["chunk_id"])
                if chunk:
                    chunk["score"] = r.get("score", 0.0)
                    bm25_rows.append(chunk)
        else:
            # No OpenSearch results: fall back to FTS5.
            bm25_rows = self.store.search_fts(question, doc_ids, limit=20)

        fts_rows = self._filter_chunk_acl(bm25_rows, effective)

        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}

        # BM25 召回（OpenSearch BM25 or SQLite FTS5）
        for r in fts_rows:
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 0.5
            meta[cid] = r

        # 向量召回（查询侧 embedding 与 worker 侧共享同一 provider，维度一致）。
        # query provider 失败（Qwen/OpenAICompat 不可用）→ 503 embedding_unavailable，
        # 不返回低质量答案。
        try:
            question_vec = _embed_question(self.query_provider, question)
        except EmbeddingUnavailableError as exc:
            import time as _t
            trace_id = f"trace_{int(_t.time() * 1000)}_embed_unavailable"
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "embedding_unavailable",
                    "message": str(exc),
                    "cause": exc.cause,
                    "trace_id": trace_id,
                },
            ) from exc

        best_vec: dict[str, float] = {}

        # Try Milvus vector search first (if available and has data).
        # Falls back to SQLite in-memory cosine when Milvus is a stub or returns nothing.
        milvus_hits: list[dict] = []
        _used_milvus = False
        if self.vector_store is not None:
            try:
                # Build filter expression for doc_id filtering
                filter_expr = _build_milvus_filter(doc_ids)
                milvus_hits = self.vector_store.search(
                    "chunks", question_vec, top_k=max(20, top_k * 3), filter_expr=filter_expr,
                )
                _used_milvus = len(milvus_hits) > 0
            except Exception:
                _used_milvus = False

        if _used_milvus:
            # Milvus returned results: use distance directly as confidence signal.
            for hit in milvus_hits:
                cid = hit["chunk_id"]
                # cosine distance → [-1, 1]; normalize to [0, 1] via (sim+1)/2
                sim = hit.get("distance", 0.0)
                if sim > best_vec.get(cid, -2.0):
                    best_vec[cid] = sim
                    meta.setdefault(cid, hit)
        else:
            # Fallback: SQLite vector recall (load chunks and compute cosine in Python).
            vec_rows = self._filter_chunk_acl(
                self.store.list_chunks_for_vector_recall(doc_ids), effective
            )
            for r in vec_rows:
                sim = _cosine(question_vec, r["embedding"])
                if sim > best_vec.get(r["chunk_id"], -2.0):
                    best_vec[r["chunk_id"]] = sim
                    meta.setdefault(r["chunk_id"], r)
        max_sim = max(best_vec.values()) if best_vec else 0.0
        # 阈值：FTS 无命中时视为纯向量召回，Qwen 实测：
        # 语义不相关时余弦 < 0.65，弱相关 0.65-0.8，强相关 ≥ 0.8。
        # 设 0.7 作为纯向量召回的下限，避免误命中语义无关的 chunk。
        # FTS 命中时不受此限（FTS 已含 token 级匹配证据）。
        _VEC_ONLY_THRESHOLD = 0.7
        for cid, sim in best_vec.items():
            norm = (sim + 1.0) / 2.0 if max_sim > 0 else 0.0
            scores[cid] = scores.get(cid, 0.0) + 0.5 * norm

        if not scores or (not bm25_rows and max_sim < _VEC_ONLY_THRESHOLD):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "no_knowledge_in_scope"},
            )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        # 引用二次校验：再次确认每个候选 doc_id 在可见集合内
        allowed = set(doc_ids)
        hits: list[RetrievalHit] = []
        for cid, score in ranked:
            m = meta[cid]
            if m["doc_id"] not in allowed:
                continue
            title = m.get("title") or self.store.get_doc_title(m["doc_id"])
            hits.append(
                RetrievalHit(
                    chunk_id=cid,
                    doc_id=m["doc_id"],
                    doc_title=title,
                    content=m["content"],
                    section_path=m["section_path"],
                    page_no=1,
                    confidence=max(0.0, min(1.0, score)),
                )
            )
        if not hits:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "no_knowledge_in_scope"},
            )
        return hits

    def _filter_chunk_acl(
        self, rows: list[dict], effective_scopes: set[str]
    ) -> list[dict]:
        """chunk 级 ACL 过滤：
          - effective_scopes 为空 → 全部保留（已通过文档级 ACL）
          - chunk.acl_tags 为空 → 视为继承 doc（已通过文档级 ACL，保留）
          - chunk.acl_tags 非空 → 必须与 effective_scopes 相交
        """
        if not effective_scopes:
            return list(rows)
        out: list[dict] = []
        for r in rows:
            acl = r.get("acl_tags") or []
            if not acl:
                out.append(r)
                continue
            if set(acl) & effective_scopes:
                out.append(r)
        return out


def _embed_question(provider: EmbeddingProvider, question: str) -> list[float]:
    """用注入的 query provider 生成问题向量。

    与 worker 侧 chunk embedding 共享同一 provider 实现，保证维度一致、
    相同文本相似度为 1.0。stub 走纯函数确定性映射；远程 provider 走其 embed。
    """
    vectors = provider.embed([question])
    if not vectors:
        # provider 在空输入时返回空；这里兜底返回零向量，触发拒答
        return [0.0] * getattr(provider, "dim", 8)
    return vectors[0]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _build_milvus_filter(doc_ids: list[str]) -> str | None:
    """Build a Milvus filter expression to restrict search to given doc_ids.

    For Milvus, the filter syntax is: doc_id in ['id1', 'id2', ...]
    Returns None when doc_ids is empty (no filter).
    """
    if not doc_ids:
        return None
    quoted = ", ".join(f"'{did}'" for did in doc_ids)
    return f"doc_id in [{quoted}]"
