from __future__ import annotations

import math
from dataclasses import dataclass

from fastapi import HTTPException, status

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
    def __init__(self, store: SQLiteStore, top_k: int = 3) -> None:
        self.store = store
        self.top_k = top_k

    def search(self, question: str, scopes: list[str], top_k: int | None = None) -> list[RetrievalHit]:
        top_k = top_k or self.top_k
        doc_ids = self.store.list_published_doc_ids_in_scope(scopes)
        if not doc_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "no_knowledge_in_scope"},
            )

        fts_rows = self.store.search_fts(question, doc_ids, limit=20)
        vec_rows = self.store.list_chunks_for_vector_recall(doc_ids)

        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}

        # FTS5 召回（ASCII/空格分词 token 命中）
        for r in fts_rows:
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 0.5
            meta[cid] = r

        # 向量召回
        question_vec = _embed_question(question)
        best_vec: dict[str, float] = {}
        for r in vec_rows:
            sim = _cosine(question_vec, r["embedding"])
            if sim > best_vec.get(r["chunk_id"], -2.0):
                best_vec[r["chunk_id"]] = sim
                meta.setdefault(r["chunk_id"], r)
        max_sim = max(best_vec.values()) if best_vec else 0.0
        # 阈值：FTS 无命中且向量相似度过低视为无匹配
        _VEC_THRESHOLD = 0.3
        for cid, sim in best_vec.items():
            norm = (sim + 1.0) / 2.0 if max_sim > 0 else 0.0
            scores[cid] = scores.get(cid, 0.0) + 0.5 * norm

        if not scores or (not fts_rows and max_sim < _VEC_THRESHOLD):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "no_knowledge_in_scope"},
            )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        hits: list[RetrievalHit] = []
        for cid, score in ranked:
            m = meta[cid]
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
        return hits


def _embed_question(question: str) -> list[float]:
    """M1 问题侧 embedding：复用 StubEmbeddingProvider 的确定性映射。

    与 worker 的 StubEmbeddingProvider 保持一致，保证相同文本产生相同向量，
    使问题与同文本 chunk 在向量召回中相似度为 1.0。
    """
    import hashlib

    dim = 8
    digest = hashlib.sha256(question.encode("utf-8")).digest()
    values: list[float] = []
    for i in range(dim):
        lo = digest[(i * 2) % len(digest)]
        hi = digest[(i * 2 + 1) % len(digest)]
        raw = (lo << 8) | hi
        values.append((raw / 32768.0) - 1.0)
    return values


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
