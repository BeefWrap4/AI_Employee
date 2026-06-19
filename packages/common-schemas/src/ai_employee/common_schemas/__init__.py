"""Shared Pydantic schemas across AI Employee services."""

from ai_employee.common_schemas.db import (
    DB,
    Backend,
    DatabaseConfig,
    build_database_config,
    detect_backend,
    open_db,
)
from ai_employee.common_schemas.idempotency import (
    IdempotencyRecord,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
    build_idempotency_store,
)
from ai_employee.common_schemas.knowledge import (
    ChunkRecord,
    Citation,
    DocumentStatus,
    ParsedChunk,
    ParseFailedRequest,
    ParseRequest,
    ParseResponse,
)

__all__ = [
    "DB",
    "Backend",
    "ChunkRecord",
    "Citation",
    "DatabaseConfig",
    "DocumentStatus",
    "IdempotencyRecord",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "ParseFailedRequest",
    "ParseRequest",
    "ParseResponse",
    "ParsedChunk",
    "RedisIdempotencyStore",
    "build_database_config",
    "build_idempotency_store",
    "detect_backend",
    "open_db",
]
