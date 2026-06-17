from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import re

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field


SERVICE_VERSION = "0.1.0"


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl_tags: list[str] = Field(default_factory=list)


class DocumentResponse(BaseModel):
    doc_id: str
    title: str
    parse_status: str
    trace_id: str
    metadata: dict[str, Any]
    acl_tags: list[str]


class QueryRequest(BaseModel):
    session_id: str
    question: str = Field(min_length=1)
    knowledge_scopes: list[str] = Field(default_factory=list)
    stream: bool = False


class Citation(BaseModel):
    chunk_id: str
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


@dataclass
class DocumentRecord:
    doc_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    acl_tags: list[str]
    parse_status: str = "uploaded"


@dataclass
class InMemoryKnowledgeStore:
    documents: dict[str, DocumentRecord] = field(default_factory=dict)
    feedback_count: int = 0

    def create_document(self, payload: DocumentCreate) -> DocumentRecord:
        doc_id = f"doc_{len(self.documents) + 1:03d}"
        record = DocumentRecord(
            doc_id=doc_id,
            title=payload.title,
            content=payload.content,
            metadata=payload.metadata,
            acl_tags=payload.acl_tags,
        )
        self.documents[doc_id] = record
        return record

    def get_document(self, doc_id: str) -> DocumentRecord:
        try:
            return self.documents[doc_id]
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"document {doc_id} not found",
            ) from exc

    def publish_document(self, doc_id: str) -> DocumentRecord:
        record = self.get_document(doc_id)
        record.parse_status = "published"
        return record

    def find_best_document(self, query: str, knowledge_scopes: list[str]) -> DocumentRecord | None:
        candidates = [
            record
            for record in self.documents.values()
            if record.parse_status == "published"
            and _is_visible_in_scope(record, knowledge_scopes)
        ]
        if not candidates:
            return None

        query_tokens = _tokenize(query)
        return max(
            candidates,
            key=lambda record: (
                _relevance_score(record, query_tokens),
                record.doc_id,
            ),
        )

    def create_feedback(self, payload: FeedbackCreate) -> FeedbackResponse:
        self.feedback_count += 1
        return FeedbackResponse(
            feedback_id=f"fb_{self.feedback_count:03d}",
            trace_id=payload.trace_id,
            feedback_type=payload.feedback_type,
        )


def _document_response(record: DocumentRecord, trace_id: str) -> DocumentResponse:
    return DocumentResponse(
        doc_id=record.doc_id,
        title=record.title,
        parse_status=record.parse_status,
        trace_id=trace_id,
        metadata=record.metadata,
        acl_tags=record.acl_tags,
    )


def _is_visible_in_scope(record: DocumentRecord, knowledge_scopes: list[str]) -> bool:
    if not knowledge_scopes:
        return True
    visible_scopes = set(record.acl_tags)
    visible_scopes.update(str(value) for value in record.metadata.values())
    return bool(visible_scopes.intersection(knowledge_scopes))


def _tokenize(text: str) -> set[str]:
    ascii_tokens = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))
    cjk_tokens = {text[index : index + 2] for index in range(max(len(text) - 1, 0))}
    cjk_tokens.update(text[index : index + 3] for index in range(max(len(text) - 2, 0)))
    return {token.strip() for token in ascii_tokens.union(cjk_tokens) if token.strip()}


def _relevance_score(record: DocumentRecord, query_tokens: set[str]) -> int:
    searchable_text = " ".join(
        [
            record.title,
            record.content,
            " ".join(str(value) for value in record.metadata.values()),
            " ".join(record.acl_tags),
        ]
    )
    document_tokens = _tokenize(searchable_text)
    return len(query_tokens.intersection(document_tokens))


def create_app() -> FastAPI:
    app = FastAPI(title="AI Employee Knowledge API", version=SERVICE_VERSION)
    store = InMemoryKnowledgeStore()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "knowledge-api",
            "status": "ok",
            "version": SERVICE_VERSION,
        }

    @app.post(
        "/api/v1/documents",
        response_model=DocumentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_document(payload: DocumentCreate) -> DocumentResponse:
        record = store.create_document(payload)
        return _document_response(record, trace_id=f"trace_{record.doc_id}_upload")

    @app.get("/api/v1/documents/{doc_id}", response_model=DocumentResponse)
    def get_document(doc_id: str) -> DocumentResponse:
        record = store.get_document(doc_id)
        return _document_response(record, trace_id=f"trace_{doc_id}_get")

    @app.post("/api/v1/documents/{doc_id}/publish", response_model=DocumentResponse)
    def publish_document(doc_id: str) -> DocumentResponse:
        record = store.publish_document(doc_id)
        return _document_response(record, trace_id=f"trace_{doc_id}_publish")

    @app.post("/api/v1/chat/query", response_model=QueryResponse)
    def query(payload: QueryRequest) -> QueryResponse:
        record = store.find_best_document(payload.question, payload.knowledge_scopes)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no published knowledge documents available for requested scope",
            )

        trace_id = f"trace_{payload.session_id}_query"
        return QueryResponse(
            answer=(
                f"根据《{record.title}》，{record.content} "
                "该回答基于已发布知识片段生成，需结合现场数据人工确认。"
            ),
            citations=[
                Citation(
                    chunk_id=f"chunk_{record.doc_id}_001",
                    doc_title=record.title,
                    page_no=1,
                    section_path="root",
                )
            ],
            confidence=0.72,
            trace_id=trace_id,
        )

    @app.post(
        "/api/v1/feedback",
        response_model=FeedbackResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_feedback(payload: FeedbackCreate) -> FeedbackResponse:
        return store.create_feedback(payload)

    return app


app = create_app()
