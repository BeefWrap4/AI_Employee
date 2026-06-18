from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSE_FAILED = "parse_failed"
    READY = "ready"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ParsedChunk(BaseModel):
    """worker 解析产出的单条 chunk（可能附带 embedding）。"""

    chunk_id: str
    chunk_no: int
    content: str
    section_path: str = "root"
    page_no: int = 1
    embedding: list[float] | None = None
    # Table structure provenance (spec §5.2).  None for plain prose.
    table_id: str | None = None
    row_id: str | None = None


class ChunkRecord(BaseModel):
    """落库后的 chunk 持久化视图。"""

    chunk_id: str
    doc_id: str
    chunk_no: int
    content: str
    section_path: str = "root"
    page_no: int = 1
    embedding: list[float] | None = None
    embedding_model: str | None = None
    # Table structure provenance (spec §5.2).  None for plain prose.
    table_id: str | None = None
    row_id: str | None = None


class Citation(BaseModel):
    chunk_id: str
    doc_title: str
    page_no: int
    section_path: str


class ParseRequest(BaseModel):
    doc_id: str
    file_path: str
    mime_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParseResponse(BaseModel):
    doc_id: str
    chunks: list[ParsedChunk]
    embeddings: list[list[float]] = Field(default_factory=list)
    embedding_model: str | None = None


class ParseFailedRequest(BaseModel):
    """worker 回写解析失败时携带的负载。"""

    doc_id: str
    parse_error: str
    stage: str
