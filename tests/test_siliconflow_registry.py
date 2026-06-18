"""SiliconFlow model registry + routing tests (R15-2)."""
from __future__ import annotations

import pytest
from ai_employee.llm_gateway.model_registry import (
    build_siliconflow_client_for_task,
    build_url_for_task,
    get_model_for_task,
    is_chat_model,
    is_embed_model,
    is_rerank_model,
    list_models,
    list_models_for_task,
)

# --------------------------------------------------------------------------- #
# Registry shape
# --------------------------------------------------------------------------- #


def test_list_models_returns_at_least_one_per_task() -> None:
    models = list_models()
    tasks = {m.task for m in models}
    assert {"chat", "embed", "rerank"}.issubset(tasks)


def test_list_models_for_task_returns_canonical_chat() -> None:
    specs = list_models_for_task("chat")
    assert any("Qwen" in s.model_id for s in specs)


def test_list_models_for_task_returns_bge_m3() -> None:
    """BAAI/bge-m3 is the spec-mandated embedding model (§5.4)."""
    specs = list_models_for_task("embed")
    assert any("bge-m3" in s.model_id.lower() for s in specs)
    assert any(s.dimensions == 1024 for s in specs)


def test_list_models_for_task_returns_bge_reranker() -> None:
    """bge-reranker-v2-m3 is the spec-mandated rerank model (§5.4)."""
    specs = list_models_for_task("rerank")
    assert any("reranker" in s.model_id.lower() for s in specs)


# --------------------------------------------------------------------------- #
# Resolution rules
# --------------------------------------------------------------------------- #


def test_get_model_for_task_returns_default() -> None:
    spec = get_model_for_task("chat")
    assert "Qwen" in spec.model_id
    assert spec.url_path == "/chat/completions"


def test_get_model_for_task_preferred_override() -> None:
    spec = get_model_for_task(
        "chat", preferred="deepseek-ai/DeepSeek-V3",
    )
    assert spec.model_id == "deepseek-ai/DeepSeek-V3"


def test_get_model_for_task_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SILICONFLOW_CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    spec = get_model_for_task("chat")
    assert spec.model_id == "Qwen/Qwen2.5-7B-Instruct"


def test_get_model_for_task_unknown_task_raises() -> None:
    with pytest.raises(KeyError) as ei:
        get_model_for_task("translate")
    assert "translate" in str(ei.value)


def test_get_model_for_task_preferred_not_in_catalog_falls_back() -> None:
    """Unknown preferred model falls back to default (no exception)."""
    spec = get_model_for_task("chat", preferred="nonexistent/model")
    assert "Qwen" in spec.model_id


# --------------------------------------------------------------------------- #
# URL building
# --------------------------------------------------------------------------- #


def test_build_url_for_task_chat() -> None:
    url = build_url_for_task("chat", base_url="https://api.siliconflow.cn/v1")
    assert url == "https://api.siliconflow.cn/v1/chat/completions"


def test_build_url_for_task_embed() -> None:
    url = build_url_for_task("embed", base_url="https://api.siliconflow.cn/v1")
    assert url == "https://api.siliconflow.cn/v1/embeddings"


def test_build_url_for_task_rerank() -> None:
    url = build_url_for_task("rerank", base_url="https://api.siliconflow.cn/v1")
    assert url == "https://api.siliconflow.cn/v1/rerank"


def test_build_url_for_task_strips_trailing_slash() -> None:
    url = build_url_for_task("chat", base_url="https://api.siliconflow.cn/v1/")
    assert url == "https://api.siliconflow.cn/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Task-aware client factory
# --------------------------------------------------------------------------- #


def test_build_siliconflow_client_for_task_picks_default_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    client = build_siliconflow_client_for_task("chat")
    assert "Qwen" in client.model


def test_build_siliconflow_client_for_task_picks_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    client = build_siliconflow_client_for_task("embed")
    assert "bge-m3" in client.model.lower()


def test_build_siliconflow_client_for_task_picks_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    client = build_siliconflow_client_for_task("rerank")
    assert "reranker" in client.model.lower()


def test_build_siliconflow_client_for_task_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    client = build_siliconflow_client_for_task(
        "chat", preferred="deepseek-ai/DeepSeek-V3",
    )
    assert client.model == "deepseek-ai/DeepSeek-V3"


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #


def test_is_chat_model_true() -> None:
    assert is_chat_model("Qwen/Qwen2.5-72B-Instruct") is True


def test_is_chat_model_false() -> None:
    assert is_chat_model("BAAI/bge-m3") is False


def test_is_embed_model_true() -> None:
    assert is_embed_model("BAAI/bge-m3") is True


def test_is_rerank_model_true() -> None:
    assert is_rerank_model("BAAI/bge-reranker-v2-m3") is True


def test_is_rerank_model_false() -> None:
    assert is_rerank_model("BAAI/bge-m3") is False


# --------------------------------------------------------------------------- #
# ModelSpec fields
# --------------------------------------------------------------------------- #


def test_modelspec_has_dimensions_for_embed_only() -> None:
    embed = get_model_for_task("embed")
    chat = get_model_for_task("chat")
    rerank = get_model_for_task("rerank")
    assert embed.dimensions == 1024
    assert chat.dimensions is None
    assert rerank.dimensions is None
