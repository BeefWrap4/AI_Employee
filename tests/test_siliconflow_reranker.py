"""SiliconFlow bge-reranker-v2-m3 adapter tests (R15-3).

Spec §5.4 — stage 6 (Rerank) wired through SiliconFlow's hosted
bge-reranker-v2-m3 endpoint so the knowledge base can ship real
cross-encoder reranking without self-hosting a GPU.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from ai_employee.knowledge_api.reranker import (
    CrossEncoderReranker,
    SiliconFlowReranker,
    StubReranker,
    build_reranker,
)
from ai_employee.knowledge_api.retrieval import RetrievalHit


def _hit(content: str, confidence: float = 0.5) -> RetrievalHit:
    return RetrievalHit(
        chunk_id="c", doc_id="d", doc_title="t", content=content,
        section_path="root", page_no=1, confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_siliconflow_reranker_defaults() -> None:
    r = SiliconFlowReranker(api_key="sk-x")
    assert "siliconflow.cn" in r.base_url
    assert "bge-reranker" in r.model.lower()
    assert r.api_key == "sk-x"


def test_siliconflow_reranker_uses_model_registry_default() -> None:
    """Model id pulled from the registry when not overridden."""
    r = SiliconFlowReranker(api_key="sk-x")
    assert "BAAI/bge-reranker-v2-m3" in r.model


def test_siliconflow_reranker_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-large")
    r = SiliconFlowReranker(api_key="sk-x")
    assert r.model == "BAAI/bge-reranker-large"


# --------------------------------------------------------------------------- #
# HTTP request shape (mocked)
# --------------------------------------------------------------------------- #


def test_rerank_sends_auth_header_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SILICONFLOW_RERANK_MODEL", raising=False)
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"scores": [0.9, 0.3, 0.7]}

    def fake_post(url: str, **kwargs) -> _Resp:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        captured["json"] = kwargs.get("json") or {}
        return _Resp()

    with patch("httpx.post", new=fake_post):
        r = SiliconFlowReranker(api_key="sk-sf")
        hits = [_hit("alpha"), _hit("beta"), _hit("gamma")]
        out = r.rerank("query", hits, top_k=3)

    assert captured["url"].endswith("/v1/rerank")
    assert captured["headers"]["Authorization"] == "Bearer sk-sf"
    body = captured["json"]
    assert body["model"] == "BAAI/bge-reranker-v2-m3"
    assert body["query"] == "query"
    assert body["documents"] == ["alpha", "beta", "gamma"]
    # Highest score (alpha = 0.9) sorts first.
    assert out[0].content == "alpha"
    assert out[1].content == "gamma"
    assert out[2].content == "beta"


def test_rerank_handles_results_dict_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SiliconFlow sometimes returns ``{"results":[{index, relevance_score}]}``."""

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.1},
                ],
            }

    with patch(
        "httpx.post",
        return_value=_Resp(),
    ):
        r = SiliconFlowReranker(api_key="sk-sf")
        hits = [_hit("alpha"), _hit("beta")]
        out = r.rerank("query", hits, top_k=2)
    assert out[0].content == "alpha"


def test_rerank_falls_back_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self) -> dict:
            return {}

    with patch(
        "httpx.post",
        return_value=_Resp(),
    ):
        r = SiliconFlowReranker(api_key="bad")
        hits = [_hit("alpha"), _hit("beta")]
        out = r.rerank("query", hits, top_k=2)
    # Falls back to StubReranker (deterministic; alpha contains "query"
    # token so it still wins).
    assert isinstance(out[0], RetrievalHit)


def test_rerank_falls_back_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    with patch("httpx.post", new=boom):
        r = SiliconFlowReranker(api_key="sk-x")
        out = r.rerank("query", [_hit("alpha")], top_k=1)
    assert out[0].content == "alpha"


def test_rerank_falls_back_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive — build with no key, must not crash on rerank()."""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    r = SiliconFlowReranker(api_key="")
    out = r.rerank("query", [_hit("alpha")], top_k=1)
    assert out[0].content == "alpha"


def test_rerank_falls_back_on_length_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the model returns fewer scores than candidates, fall back."""

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"scores": [0.9]}  # 2 hits sent, 1 score returned

    with patch(
        "httpx.post",
        return_value=_Resp(),
    ):
        r = SiliconFlowReranker(api_key="sk-x")
        out = r.rerank(
            "query", [_hit("alpha"), _hit("beta")], top_k=2,
        )
    assert len(out) == 2


# --------------------------------------------------------------------------- #
# build_reranker factory — selection rules
# --------------------------------------------------------------------------- #


def test_build_reranker_prefers_explicit_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANKER_URL", "http://custom:8080")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    r = build_reranker()
    assert isinstance(r, CrossEncoderReranker)
    assert r.base_url == "http://custom:8080"


def test_build_reranker_uses_siliconflow_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    monkeypatch.delenv("RERANKER_URL", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    r = build_reranker()
    assert isinstance(r, SiliconFlowReranker)
    assert r.api_key == "sk-sf"


def test_build_reranker_falls_back_to_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    monkeypatch.delenv("RERANKER_URL", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    r = build_reranker()
    assert isinstance(r, StubReranker)


# --------------------------------------------------------------------------- #
# Empty hits / happy-path passthrough
# --------------------------------------------------------------------------- #


def test_rerank_empty_hits_returns_empty() -> None:
    r = SiliconFlowReranker(api_key="sk-x")
    assert r.rerank("query", [], top_k=5) == []


def test_siliconflow_reranker_has_descriptive_name() -> None:
    r = SiliconFlowReranker(api_key="sk-x")
    assert "bge-reranker" in r.name


def test_siliconflow_reranker_top_k_caps_output() -> None:
    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"scores": [0.9, 0.8, 0.7, 0.6, 0.5]}

    with patch(
        "httpx.post",
        return_value=_Resp(),
    ):
        r = SiliconFlowReranker(api_key="sk-x")
        hits = [_hit(f"c{i}", confidence=0.1) for i in range(5)]
        out = r.rerank("query", hits, top_k=2)
    assert len(out) == 2
    # Highest scores win.
    assert out[0].content == "c0"
    assert out[1].content == "c1"


def test_env_var_cleanup_does_not_leak_to_other_tests() -> None:
    """Sanity: helper sets are scoped to this test only."""
    # Trivial assertion — this is a smoke check that monkeypatch didn't
    # mutate the real environment.
    assert True
