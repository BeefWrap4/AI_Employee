"""Shared Pydantic schemas across AI Employee services."""

from ai_employee.common_schemas.db import (
    DB,
    Backend,
    DatabaseConfig,
    build_database_config,
    detect_backend,
    open_db,
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
    "ParseFailedRequest",
    "ParseRequest",
    "ParseResponse",
    "ParsedChunk",
    "build_database_config",
    "detect_backend",
    "open_db",
]
