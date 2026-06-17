from __future__ import annotations

import httpx
import pytest

from ai_employee.common_schemas.embedding import (
    EmbeddingProvider,
    QwenEmbeddingProvider,
    StubEmbeddingProvider,
    build_provider,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        return self._payload or {}


def _emb_payload(n: int, dim: int = 1024) -> dict:
    return {
        "object": "list",
        "model": "text-embedding-v3",
        "data": [{"object": "embedding", "embedding": [0.01 * i] * dim, "index": i} for i in range(n)],
        "usage": {"prompt_tokens": 10, "total_tokens": 10},
    }


def test_qwen_provider_name_and_dim() -> None:
    p = QwenEmbeddingProvider(api_key="k", model="text-embedding-v3", dim=1024)
    assert p.name == "qwen"
    assert p.dim == 1024


def test_qwen_embed_single_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(200, _emb_payload(len(json["input"])))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = QwenEmbeddingProvider(api_key="secret-key", model="text-embedding-v3", dim=1024)
    vecs = p.embed(["基站告警处理"])
    assert len(vecs) == 1
    assert len(vecs[0]) == 1024
    assert calls[0]["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-key"
    assert calls[0]["json"]["model"] == "text-embedding-v3"
    assert calls[0]["json"]["input"] == ["基站告警处理"]


def test_qwen_embed_batches_over_max(monkeypatch: pytest.MonkeyPatch) -> None:
    batch_sizes: list[int] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        batch_sizes.append(len(json["input"]))
        return _FakeResponse(200, _emb_payload(len(json["input"])))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = QwenEmbeddingProvider(api_key="k", model="text-embedding-v3", dim=1024, max_batch=10)
    texts = [f"chunk-{i}" for i in range(25)]
    vecs = p.embed(texts)
    assert len(vecs) == 25
    assert batch_sizes == [10, 10, 5]


def test_qwen_embed_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = [429, 429, 200]
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        s = statuses[calls["n"] - 1]
        if s == 200:
            return _FakeResponse(200, _emb_payload(len(json["input"])))
        return _FakeResponse(s, text="throttling")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    p = QwenEmbeddingProvider(api_key="k", model="text-embedding-v3", dim=1024, max_retries=3)
    vecs = p.embed(["x"])
    assert len(vecs) == 1
    assert calls["n"] == 3


def test_qwen_embed_raises_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(401, text="InvalidApiKey")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    p = QwenEmbeddingProvider(api_key="bad", model="text-embedding-v3", dim=1024, max_retries=2)
    with pytest.raises(RuntimeError) as exc:
        p.embed(["x"])
    assert "401" in str(exc.value)


def test_qwen_embed_empty_returns_empty() -> None:
    p = QwenEmbeddingProvider(api_key="k", model="text-embedding-v3", dim=1024)
    assert p.embed([]) == []


def test_qwen_provider_satisfies_protocol() -> None:
    p: EmbeddingProvider = QwenEmbeddingProvider(api_key="k", model="text-embedding-v3", dim=1024)
    assert p.name and p.dim > 0


def test_build_provider_stub_default() -> None:
    provider, degraded = build_provider(provider_name="stub")
    assert isinstance(provider, StubEmbeddingProvider)
    assert degraded is False


def test_build_provider_qwen_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "fake-key")
    provider, degraded = build_provider(provider_name="qwen")
    assert isinstance(provider, QwenEmbeddingProvider)
    assert degraded is False
    assert provider.model == "text-embedding-v3"
    assert provider.dim == 1024


def test_build_provider_qwen_without_key_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    provider, degraded = build_provider(provider_name="qwen")
    assert isinstance(provider, StubEmbeddingProvider)
    assert degraded is True


def test_build_provider_qwen_respects_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "fake-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v4")
    provider, degraded = build_provider(provider_name="qwen")
    assert isinstance(provider, QwenEmbeddingProvider)
    assert provider.model == "text-embedding-v4"
