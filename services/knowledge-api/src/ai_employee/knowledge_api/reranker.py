"""Reranker — second-stage re-ranking of retrieval candidates (spec §5.4
stage 6: Rerank).

A reranker takes the query and the Top-K fused candidates and produces a
refined ordering that is more precise than raw vector+BM25 fusion.  This
module ships three implementations:

* :class:`StubReranker` — deterministic, zero-dependency.  Scores each
  candidate by query/candidate token overlap (Jaccard) plus a bonus for
  candidates whose content mentions an extracted alarm code or entity.
  This is NOT a cross-encoder but gives a measurable, testable lift over
  raw fusion and keeps the MVP dependency-free.
* :class:`CrossEncoderReranker` — adapter for a bge-reranker-v2-m3 style
  model served behind a generic ``{"query":..., "documents":[...]}``
  endpoint.  Selected when ``RERANKER_ENABLED=true`` + ``RERANKER_URL``
  are set; otherwise the stub is used.
* :class:`SiliconFlowReranker` — adapter that POSTs the SiliconFlow
  rerank endpoint (``https://api.siliconflow.cn/v1/rerank``) with
  ``{"model": "BAAI/bge-reranker-v2-m3", "query":..., "documents":[...]}``
  and an ``Authorization: Bearer <SILICONFLOW_API_KEY>`` header.

All implement :func:`Reranker.rerank` returning a new sorted list of
``RetrievalHit`` with updated ``confidence``.  Any adapter falls back to
the stub on transport errors so retrieval never breaks because the
reranker is unavailable.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Protocol

from ai_employee.knowledge_api.query_normalize import extract_entities
from ai_employee.knowledge_api.retrieval import RetrievalHit


class Reranker(Protocol):
    name: str

    def rerank(self, question: str, hits: list[RetrievalHit], top_k: int) -> list[RetrievalHit]: ...


def _tokenize(text: str) -> set[str]:
    return {t for t in text.lower().split() if t}


class StubReranker:
    """Deterministic token-overlap + entity-bonus reranker."""

    name = "stub.reranker"

    def rerank(self, question: str, hits: list[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        if not hits:
            return []
        q_tokens = _tokenize(question)
        ents = extract_entities(question)
        entity_terms = {
            t.lower()
            for ent_list in (
                ents.alarm_codes,
                ents.ne_ids,
                ents.cell_ids,
                ents.site_ids,
                ents.vendors,
                ents.network_types,
                ents.metrics,
            )
            for t in ent_list
        }
        scored: list[tuple[float, RetrievalHit]] = []
        for hit in hits:
            c_tokens = _tokenize(hit.content)
            union = q_tokens | c_tokens
            jaccard = (len(q_tokens & c_tokens) / len(union)) if union else 0.0
            content_lower = hit.content.lower()
            entity_bonus = sum(0.15 for term in entity_terms if term in content_lower)
            # Blend: keep some of the original fusion confidence so a
            # high-vector match isn't fully discarded.
            rerank_score = 0.5 * hit.confidence + 0.3 * jaccard + min(entity_bonus, 0.45)
            scored.append((rerank_score, hit))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        out: list[RetrievalHit] = []
        for score, hit in scored[:top_k]:
            out.append(replace(hit, confidence=max(0.0, min(1.0, score))))
        return out


class CrossEncoderReranker:
    """Adapter for a remote cross-encoder reranker (bge-reranker-v2-m3).

    Expects an endpoint that accepts ``{"query":..., "documents":[...]}``
    and returns ``{"scores": [float, ...]}``.  Falls back to
    :class:`StubReranker` behaviour on any error so retrieval never
    breaks because the reranker is unavailable.
    """

    name = "crossencoder.reranker"

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._fallback = StubReranker()

    def rerank(self, question: str, hits: list[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        if not hits:
            return []
        import httpx

        try:
            resp = httpx.post(
                f"{self.base_url}/rerank",
                json={
                    "query": question,
                    "documents": [h.content for h in hits],
                },
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                return self._fallback.rerank(question, hits, top_k)
            scores = (resp.json() or {}).get("scores", [])
        except Exception:
            return self._fallback.rerank(question, hits, top_k)
        if len(scores) != len(hits):
            return self._fallback.rerank(question, hits, top_k)
        paired = sorted(zip(scores, hits, strict=False), key=lambda kv: kv[0], reverse=True)
        out: list[RetrievalHit] = []
        for score, hit in paired[:top_k]:
            out.append(replace(hit, confidence=max(0.0, min(1.0, float(score)))))
        return out


class SiliconFlowReranker:
    """Adapter for the SiliconFlow (硅基流动) bge-reranker-v2-m3 endpoint.

    Sends:

        POST {base}/rerank
        Authorization: Bearer <SILICONFLOW_API_KEY>
        {"model": "BAAI/bge-reranker-v2-m3", "query":..., "documents":[...]}

    Returns ``{"scores": [...]}`` shaped like the generic cross-encoder
    adapter, so the rest of the pipeline doesn't care which backend
    produced the scores.  Falls back to the stub on any error so a
    SiliconFlow outage doesn't break retrieval.
    """

    name = "siliconflow.bge-reranker-v2-m3"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        from ai_employee.llm_gateway.model_registry import get_model_for_task

        self.api_key = api_key or os.getenv("SILICONFLOW_API_KEY", "")
        self.base_url = (
            base_url or os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        ).rstrip("/")
        self.model = (
            model or os.getenv("SILICONFLOW_RERANK_MODEL") or get_model_for_task("rerank").model_id
        )
        self._timeout = timeout_seconds
        self._fallback = StubReranker()

    def rerank(self, question: str, hits: list[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        if not hits:
            return []
        if not self.api_key:
            return self._fallback.rerank(question, hits, top_k)
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "query": question,
            "documents": [h.content for h in hits],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/rerank",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except Exception:
            return self._fallback.rerank(question, hits, top_k)
        if resp.status_code >= 400:
            return self._fallback.rerank(question, hits, top_k)
        try:
            body = resp.json() or {}
            scores = body.get("scores") or body.get("results", [])
            # SiliconFlow may return {"results":[{"index":..,"relevance_score":..}, ...]}
            if scores and isinstance(scores[0], dict):
                scores = [item.get("relevance_score") for item in scores]
        except Exception:
            return self._fallback.rerank(question, hits, top_k)
        if not isinstance(scores, list) or len(scores) != len(hits):
            return self._fallback.rerank(question, hits, top_k)
        paired = sorted(zip(scores, hits, strict=False), key=lambda kv: kv[0], reverse=True)
        out: list[RetrievalHit] = []
        for score, hit in paired[:top_k]:
            try:
                fscore = float(score)
            except (TypeError, ValueError):
                return self._fallback.rerank(question, hits, top_k)
            out.append(replace(hit, confidence=max(0.0, min(1.0, fscore))))
        return out


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def build_reranker() -> Reranker:
    """Pick a reranker based on env flags; default to the deterministic stub.

    Resolution order:

    1. ``RERANKER_URL`` env (explicit override; takes precedence).
    2. ``SILICONFLOW_API_KEY`` set → :class:`SiliconFlowReranker` against
       the canonical bge-reranker-v2-m3 endpoint so the knowledge base
       gets spec §5.4 stage-6 reranking for free.
    3. None of the above → :class:`StubReranker` (deterministic, MVP).
    """
    if _truthy(os.getenv("RERANKER_ENABLED")) and os.getenv("RERANKER_URL"):
        return CrossEncoderReranker(os.getenv("RERANKER_URL", ""))
    if os.getenv("SILICONFLOW_API_KEY"):
        return SiliconFlowReranker()
    return StubReranker()


__all__ = [
    "CrossEncoderReranker",
    "Reranker",
    "SiliconFlowReranker",
    "StubReranker",
    "build_reranker",
]
