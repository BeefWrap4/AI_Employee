from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DocumentResponse(BaseModel):
    doc_id: str
    title: str
    mime_type: str
    parse_status: str
    parse_error: str | None = None
    chunk_count: int
    version: str
    trace_id: str
    metadata: dict[str, Any]
    acl_tags: list[str]
    worker_dispatch: str | None = None
    updated_at: str | None = None


class ChunkResponse(BaseModel):
    chunk_id: str
    content: str
    page_no: int
    section_path: str


class DocumentChunksResponse(BaseModel):
    doc_id: str
    chunks: list[ChunkResponse]


class QueryRequest(BaseModel):
    session_id: str
    question: str = Field(min_length=1)
    knowledge_scopes: list[str] = Field(default_factory=list)
    stream: bool = False


class Citation(BaseModel):
    chunk_id: str
    doc_id: str
    doc_title: str
    page_no: int
    section_path: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    confidence: float
    trace_id: str


class FeedbackCreate(BaseModel):
    trace_id: str
    feedback_type: str
    comment: str | None = None


class FeedbackResponse(BaseModel):
    feedback_id: str
    trace_id: str
    feedback_type: str


class InternalChunksRequest(BaseModel):
    """worker 回写 chunk 的负载。"""

    doc_id: str
    chunks: list[dict]
    embeddings: list[list[float]]
    embedding_model: str


class InternalParseFailedRequest(BaseModel):
    doc_id: str
    parse_error: str
    stage: str


# ===== 审计端点响应模型（M2.1）=====

class QaLogSummary(BaseModel):
    qa_log_id: str
    trace_id: str
    session_id: str
    user_id: str | None
    question: str
    knowledge_scopes: list[str] = Field(default_factory=list)
    answer: str
    confidence: float
    latency_ms: int
    model_name: str
    created_at: str


class QaLogResponse(QaLogSummary):
    """单条 qa_log 详情。retrieved_chunks 由 store 层从 qa_logs.retrieved_chunks_json
    列反序列化得到（list[dict]），含 chunk_id / doc_id / 命中顺序。"""
    retrieved_chunks: list[dict]


class QaLogListResponse(BaseModel):
    items: list[QaLogSummary]
    total: int
    page: int
    page_size: int


class FeedbackSummary(BaseModel):
    feedback_id: str
    trace_id: str
    feedback_type: str
    comment: str | None
    user_id: str | None
    created_at: str


class FeedbackListResponse(BaseModel):
    items: list[FeedbackSummary]
    total: int
    page: int
    page_size: int


class DocumentSummary(BaseModel):
    doc_id: str
    title: str
    mime_type: str
    parse_status: str
    chunk_count: int
    version: str
    created_at: str
    updated_at: str


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int
    page: int
    page_size: int
