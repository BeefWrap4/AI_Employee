"""Backward-compatible re-export of embedding providers.

The canonical implementations live in ``ai_employee.common_schemas.embedding``
so that both ingestion-worker (chunk-side) and knowledge-api (query-side) share
identical providers and produce dimensionally-compatible vectors. This module
re-exports them for existing call sites and tests.
"""

from __future__ import annotations

from ai_employee.common_schemas.embedding import (
    EmbeddingProvider,
    OpenAICompatEmbeddingProvider,
    QwenEmbeddingProvider,
    StubEmbeddingProvider,
    build_provider,
)

__all__ = [
    "EmbeddingProvider",
    "OpenAICompatEmbeddingProvider",
    "QwenEmbeddingProvider",
    "StubEmbeddingProvider",
    "build_provider",
]
