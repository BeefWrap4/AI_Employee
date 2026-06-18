"""SiliconFlow integration smoke tests (R15-5).

End-to-end shape of the R15 wiring without hitting the network:

* ``build_siliconflow_client`` + ``build_siliconflow_client_for_task``
  all return a properly-configured ``SiliconFlowClient``.
* ``SiliconFlowReranker`` + the new ``siliconflow`` ``EMBEDDING_PROVIDER``
  share the same base URL and accept the same key.
* ``build_reranker`` selects the SiliconFlow reranker when only the key
  is set (the "free" upgrade path).
* Env override precedence works for chat / embed / rerank simultaneously.
"""
from __future__ import annotations

import pytest
from ai_employee.common_schemas.embedding import (
    OpenAICompatEmbeddingProvider,
    build_provider,
)
from ai_employee.knowledge_api.reranker import (
    SiliconFlowReranker,
    build_reranker,
)
from ai_employee.llm_gateway.client import (
    SiliconFlowClient,
    build_siliconflow_client,
)
from ai_employee.llm_gateway.model_registry import (
    build_siliconflow_client_for_task,
    get_model_for_task,
)


def test_full_stack_with_siliconflow_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With SILICONFLOW_API_KEY set, every layer points at siliconflow.cn."""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf-smoke")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    monkeypatch.delenv("RERANKER_URL", raising=False)

    # LLM chat
    chat_client = build_siliconflow_client_for_task("chat")
    assert isinstance(chat_client, SiliconFlowClient)
    assert "siliconflow.cn" in chat_client.base_url
    assert "Qwen" in chat_client.model
    assert chat_client.api_key == "sk-sf-smoke"

    # Embedding
    embed_provider, degraded = build_provider()
    assert degraded is False
    assert isinstance(embed_provider, OpenAICompatEmbeddingProvider)
    assert "siliconflow.cn" in embed_provider.base_url
    assert "bge-m3" in embed_provider.model.lower()

    # Reranker
    reranker = build_reranker()
    assert isinstance(reranker, SiliconFlowReranker)
    assert reranker.api_key == "sk-sf-smoke"
    assert "siliconflow.cn" in reranker.base_url


def test_env_override_per_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """SILICONFLOW_<TASK>_MODEL overrides resolve correctly per layer."""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    monkeypatch.setenv("SILICONFLOW_CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    monkeypatch.setenv("SILICONFLOW_EMBED_MODEL", "BAAI/bge-large-zh-v1.5")
    monkeypatch.setenv("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-large")

    chat = build_siliconflow_client_for_task("chat")
    assert chat.model == "Qwen/Qwen2.5-7B-Instruct"

    # Embedding side pulls EMBEDDING_MODEL.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    embed, _ = build_provider()
    assert embed.model == "BAAI/bge-large-zh-v1.5"

    # Reranker side uses registry / env override.
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    monkeypatch.delenv("RERANKER_URL", raising=False)
    reranker = build_reranker()
    assert reranker.model == "BAAI/bge-reranker-large"


def test_smoke_no_key_means_stub_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key → all three layers degrade (client raises, embed stub, rerank stub)."""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    monkeypatch.delenv("RERANKER_URL", raising=False)

    # Chat factory fails fast.
    with pytest.raises(RuntimeError):
        build_siliconflow_client()
    with pytest.raises(RuntimeError):
        build_siliconflow_client_for_task("chat")

    # Embedding falls back to stub.
    from ai_employee.common_schemas.embedding import StubEmbeddingProvider
    embed, degraded = build_provider()
    assert degraded is False  # default provider is stub (not degraded)
    assert isinstance(embed, StubEmbeddingProvider)

    # Reranker falls back to stub.
    from ai_employee.knowledge_api.reranker import StubReranker
    reranker = build_reranker()
    assert isinstance(reranker, StubReranker)


def test_registry_chat_default_is_72b(monkeypatch: pytest.MonkeyPatch) -> None:
    """72B is the default; 7B only when overridden."""
    monkeypatch.delenv("SILICONFLOW_CHAT_MODEL", raising=False)
    spec = get_model_for_task("chat")
    assert spec.model_id == "Qwen/Qwen2.5-72B-Instruct"


def test_embed_dim_is_1024(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bge-m3 spec carries the canonical 1024 dim."""
    spec = get_model_for_task("embed")
    assert spec.dimensions == 1024


def test_all_three_endpoints_have_documented_url_paths() -> None:
    """Sanity: chat/embed/rerank all have correct paths."""
    from ai_employee.llm_gateway.model_registry import (
        build_url_for_task,
    )
    base = "https://api.siliconflow.cn/v1"
    assert build_url_for_task("chat", base_url=base) == f"{base}/chat/completions"
    assert build_url_for_task("embed", base_url=base) == f"{base}/embeddings"
    assert build_url_for_task("rerank", base_url=base) == f"{base}/rerank"
