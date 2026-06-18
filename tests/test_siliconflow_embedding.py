"""SiliconFlow BAAI/bge-m3 embedding provider tests (R15-4).

Spec §5.4 — knowledge-api can use SiliconFlow's hosted bge-m3
embeddings (1024-dim multilingual) instead of the local stub.

The provider is selected by setting ``EMBEDDING_PROVIDER=siliconflow``
and ``SILICONFLOW_API_KEY=...``; ``build_provider`` returns an
OpenAI-compatible client pointed at ``api.siliconflow.cn/v1/embeddings``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from ai_employee.common_schemas.embedding import (
    EmbeddingUnavailableError,
    OpenAICompatEmbeddingProvider,
    StubEmbeddingProvider,
    build_provider,
)

# --------------------------------------------------------------------------- #
# Factory selection
# --------------------------------------------------------------------------- #


def test_siliconflow_provider_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    provider, degraded = build_provider()
    assert degraded is False
    assert isinstance(provider, OpenAICompatEmbeddingProvider)
    assert "bge-m3" in provider.model.lower()
    assert "siliconflow.cn" in provider.base_url
    assert provider.api_key == "sk-sf"
    assert provider.dim == 1024


def test_siliconflow_provider_falls_back_to_stub_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    provider, degraded = build_provider()
    assert degraded is True
    assert isinstance(provider, StubEmbeddingProvider)


def test_siliconflow_provider_honors_env_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")
    provider, _ = build_provider()
    assert provider.model == "BAAI/bge-large-zh-v1.5"


def test_siliconflow_provider_honors_dim_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    provider, _ = build_provider()
    assert provider.dim == 768


def test_siliconflow_provider_honors_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setenv("SILICONFLOW_BASE_URL", "http://localhost:9999")
    provider, _ = build_provider()
    assert provider.base_url == "http://localhost:9999"


# --------------------------------------------------------------------------- #
# HTTP request shape (mocked)
# --------------------------------------------------------------------------- #


def test_siliconflow_provider_sends_bge_m3_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {
                "data": [
                    {"embedding": [0.1] * 1024},
                    {"embedding": [0.2] * 1024},
                ],
            }

    def fake_post(url: str, **kwargs) -> _Resp:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        captured["json"] = kwargs.get("json") or {}
        return _Resp()

    with patch("httpx.post", new=fake_post):
        provider, _ = build_provider()
        vecs = provider.embed(["hello", "world"])

    assert captured["url"].endswith("/v1/embeddings")
    assert captured["headers"]["Authorization"] == "Bearer sk-sf"
    body = captured["json"]
    assert body["model"] == "BAAI/bge-m3"
    assert body["input"] == ["hello", "world"]
    assert len(vecs) == 2
    assert len(vecs[0]) == 1024


def test_siliconflow_provider_propagates_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")

    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self) -> dict:
            return {}

    with patch("httpx.post", return_value=_Resp()):
        provider, _ = build_provider()
        with pytest.raises(EmbeddingUnavailableError) as ei:
            provider.embed(["x"])
        assert ei.value.cause == "4xx"


def test_siliconflow_provider_propagates_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")

    class _Resp:
        status_code = 503
        text = "service unavailable"

        def json(self) -> dict:
            return {}

    with patch("httpx.post", return_value=_Resp()):
        provider, _ = build_provider()
        with pytest.raises(EmbeddingUnavailableError) as ei:
            provider.embed(["x"])
        assert ei.value.cause == "5xx"


def test_siliconflow_provider_batches_large_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec mentions Qwen's 10-batch limit; SiliconFlow is similar."""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    call_count = {"n": 0}

    def fake_post(url: str, **kwargs) -> object:
        body = kwargs.get("json") or {}
        call_count["n"] += 1
        # Each call should have at most max_batch inputs.
        assert len(body["input"]) <= 5
        # Return one embedding per input.
        resp_data = {
            "data": [
                {"embedding": [0.5] * 1024} for _ in body["input"]
            ],
        }

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self_inner) -> dict:
                return resp_data

        return _Resp()

    with patch("httpx.post", new=fake_post):
        provider = OpenAICompatEmbeddingProvider(
            base_url="https://api.siliconflow.cn",
            api_key="sk-sf",
            model="BAAI/bge-m3",
            dim=1024,
            max_batch=5,
        )
        vecs = provider.embed([f"text-{i}" for i in range(12)])
    # 12 inputs / 5 per batch = 3 calls
    assert call_count["n"] == 3
    assert len(vecs) == 12


# --------------------------------------------------------------------------- #
# Stub fallback still works (defensive)
# --------------------------------------------------------------------------- #


def test_siliconflow_provider_missing_key_returns_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When key is missing, ``build_provider`` returns the stub + degraded=True."""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    provider, degraded = build_provider()
    assert degraded is True
    assert isinstance(provider, StubEmbeddingProvider)
    # Stub still produces 8-dim vectors by default.
    assert provider.dim == 8
    assert len(provider.embed(["x"])[0]) == 8
