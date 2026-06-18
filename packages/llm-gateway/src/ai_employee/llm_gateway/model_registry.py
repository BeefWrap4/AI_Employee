"""SiliconFlow model registry + task-based routing (R15-2).

Maps platform tasks (chat, rerank, embed) to the canonical SiliconFlow
model id and the URL path used to invoke them.  Keeps the model
catalog in one place so a model upgrade is a single edit.

Tasks:

* ``chat`` — Qwen2.5-72B-Instruct (default) or Qwen2.5-7B-Instruct
  (fast).  Falls back to deepseek-v3 for code-heavy prompts.
* ``embed`` — BAAI/bge-m3 (1024-dim, multilingual, spec §5.4 default).
* ``rerank`` — BAAI/bge-reranker-v2-m3 (cross-encoder, spec §5.4
  two-stage retrieval).

Usage::

    from ai_employee.llm_gateway.model_registry import (
        get_model_for_task, build_url_for_task, build_siliconflow_client_for_task,
    )
    client = build_siliconflow_client_for_task(task="chat")

Routing rules are deterministic; tests pin the exact model id so a
silent swap would break the suite.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ai_employee.llm_gateway.client import (
    SiliconFlowClient,
    build_siliconflow_client,
)

# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelSpec:
    task: str
    model_id: str
    url_path: str
    description: str = ""
    dimensions: int | None = None  # only for embed


# Canonical SiliconFlow model catalog.  Order doesn't matter; lookup is
# by ``task`` and falls back to the default for that task.
_DEFAULT_CHAT_MODEL = "Qwen/Qwen2.5-72B-Instruct"
_FAST_CHAT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
_CODE_CHAT_MODEL = "deepseek-ai/DeepSeek-V3"
_EMBED_MODEL = "BAAI/bge-m3"
_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


_MODELS: dict[str, list[ModelSpec]] = {
    "chat": [
        ModelSpec(
            task="chat", model_id=_DEFAULT_CHAT_MODEL,
            url_path="/chat/completions",
            description="Qwen2.5-72B-Instruct — high-quality default",
        ),
        ModelSpec(
            task="chat", model_id=_FAST_CHAT_MODEL,
            url_path="/chat/completions",
            description="Qwen2.5-7B-Instruct — fast low-latency fallback",
        ),
        ModelSpec(
            task="chat", model_id=_CODE_CHAT_MODEL,
            url_path="/chat/completions",
            description="DeepSeek-V3 — code-heavy prompts",
        ),
    ],
    "embed": [
        ModelSpec(
            task="embed", model_id=_EMBED_MODEL,
            url_path="/embeddings",
            description="BAAI/bge-m3 — multilingual 1024-dim embedding",
            dimensions=1024,
        ),
    ],
    "rerank": [
        ModelSpec(
            task="rerank", model_id=_RERANK_MODEL,
            url_path="/rerank",
            description="BAAI/bge-reranker-v2-m3 — cross-encoder reranker",
        ),
    ],
}


def list_models() -> list[ModelSpec]:
    """Return the full catalog (for the dashboard / introspection)."""
    out: list[ModelSpec] = []
    for specs in _MODELS.values():
        out.extend(specs)
    return out


def list_models_for_task(task: str) -> list[ModelSpec]:
    return list(_MODELS.get(task, []))


def get_model_for_task(
    task: str,
    *,
    preferred: str | None = None,
) -> ModelSpec:
    """Resolve a :class:`ModelSpec` for ``task``.

    Lookup order:
    1. ``preferred`` argument (explicit override)
    2. ``SILICONFLOW_<TASK>_MODEL`` env var
    3. First entry in the catalog (canonical default)

    Raises :class:`KeyError` when ``task`` is unknown.
    """
    catalog = _MODELS.get(task)
    if not catalog:
        raise KeyError(f"unknown task: {task!r}")
    candidates = [s.model_id for s in catalog]

    if preferred and preferred in candidates:
        return next(s for s in catalog if s.model_id == preferred)

    env_name = f"SILICONFLOW_{task.upper()}_MODEL"
    env_value = os.getenv(env_name)
    if env_value and env_value in candidates:
        return next(s for s in catalog if s.model_id == env_value)

    return catalog[0]


# --------------------------------------------------------------------------- #
# URL building
# --------------------------------------------------------------------------- #


def build_url_for_task(task: str, *, base_url: str) -> str:
    """Return the absolute URL for a given task endpoint."""
    spec = get_model_for_task(task)
    return f"{base_url.rstrip('/')}{spec.url_path}"


# --------------------------------------------------------------------------- #
# Task-aware client factory
# --------------------------------------------------------------------------- #


def build_siliconflow_client_for_task(
    task: str,
    *,
    preferred: str | None = None,
    **kwargs: Any,
) -> SiliconFlowClient:
    """Build a SiliconFlow client pre-configured for ``task``.

    Useful when callers don't want to thread the model name through
    manually; e.g.::

        client = build_siliconflow_client_for_task("chat")
        client.chat(messages=...)

    Passes ``model=get_model_for_task(task).model_id`` plus any extra
    kwargs (timeout, max_retries, langfuse_emitter) into the factory.
    """
    spec = get_model_for_task(task, preferred=preferred)
    # Map "model" kwarg into the factory if caller didn't override.
    if "model" not in kwargs:
        kwargs["model"] = spec.model_id
    return build_siliconflow_client(**kwargs)


# --------------------------------------------------------------------------- #
# Convenience predicates
# --------------------------------------------------------------------------- #


def is_rerank_model(model_id: str) -> bool:
    return any(s.model_id == model_id for s in _MODELS.get("rerank", []))


def is_embed_model(model_id: str) -> bool:
    return any(s.model_id == model_id for s in _MODELS.get("embed", []))


def is_chat_model(model_id: str) -> bool:
    return any(s.model_id == model_id for s in _MODELS.get("chat", []))


# Silence linter (Callable kept for forward-compat extension hooks).
_ = Callable
