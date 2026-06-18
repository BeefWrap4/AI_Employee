from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from ai_employee.common_schemas.embedding import build_provider
from ai_employee.common_schemas.knowledge import DocumentStatus
from ai_employee.knowledge_api.internal_auth import require_internal_token
from ai_employee.knowledge_api.retrieval import RetrievalService
from ai_employee.knowledge_api.schemas import (
    ChunkResponse,
    Citation,
    DocumentChunksResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentSummary,
    FeedbackCreate,
    FeedbackListResponse,
    FeedbackResponse,
    FeedbackSummary,
    InternalChunksRequest,
    InternalParseFailedRequest,
    QaLogListResponse,
    QaLogResponse,
    QaLogSummary,
    QueryRequest,
    QueryResponse,
)
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

_LLM_GATEWAY_ENABLED = os.getenv("LLM_GATEWAY_ENABLED", "false").strip().lower() in (
    "true",
    "1",
    "yes",
)

SERVICE_VERSION = "0.1.0"

_MIME_EXT = {
    "text/markdown": "md",
    "text/html": "html",
    "text/plain": "txt",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
_MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "10485760"))


def _config() -> dict[str, Any]:
    data_dir = os.getenv("KNOWLEDGE_DATA_DIR", "./var/data")
    return {
        "data_dir": data_dir,
        "db_path": os.getenv("KNOWLEDGE_SQLITE_PATH", f"{data_dir}/knowledge.sqlite3"),
        "worker_url": os.getenv("INGESTION_WORKER_URL", "http://127.0.0.1:8001"),
        "worker_timeout_s": float(os.getenv("INGESTION_WORKER_TIMEOUT_S", "30")),
        "internal_token": os.getenv("KNOWLEDGE_API_INTERNAL_TOKEN", "change-me"),
    }


def create_app(
    store: SQLiteStore | None = None,
    worker_client: WorkerClient | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Knowledge API", version=SERVICE_VERSION)
    cfg = _config()
    if store is None:
        store = SQLiteStore(db_path=cfg["db_path"], data_dir=cfg["data_dir"])
        store.init_schema()
    if worker_client is None:
        worker_client = WorkerClient(
            base_url=cfg["worker_url"],
            internal_token=cfg["internal_token"],
            timeout_s=cfg["worker_timeout_s"],
        )
    # 查询侧 embedding 与 worker 侧共享同一 provider，保证维度一致
    query_provider, query_degraded = build_provider()
    retrieval = RetrievalService(store, query_provider=query_provider)
    # 暴露 retrieval 让测试可注入 query provider
    app.state.retrieval = retrieval
    auth = require_internal_token(cfg["internal_token"])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "service": "knowledge-api",
            "status": "ok",
            "version": SERVICE_VERSION,
            "storage": "sqlite",
            "ingestion_worker_reachable": worker_client.health(),
            "embedding_provider": query_provider.name,
            "embedding_provider_degraded": query_degraded,
        }

    @app.post(
        "/api/v1/documents",
        response_model=DocumentResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_document(
        file: UploadFile = File(...),
        title: str = Form(...),
        metadata_json: str = Form("{}"),
        acl_tags_json: str = Form("[]"),
        version: str = Form("v1"),
        mime_type: str | None = Form(None),
    ) -> DocumentResponse:
        content = await file.read()
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"error_code": "payload_too_large"},
            )
        declared_mime = mime_type or file.content_type or "text/plain"
        if declared_mime not in _MIME_EXT:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "error_code": "mime_unsupported",
                    "mime_type": declared_mime,
                    "supported": list(_MIME_EXT),
                },
            )
        try:
            metadata = json.loads(metadata_json)
            acl_tags = json.loads(acl_tags_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "invalid_json", "message": str(exc)},
            ) from exc

        ext = _MIME_EXT[declared_mime]
        raw_dir = os.path.join(store.data_dir, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".{ext}", dir=raw_dir)
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(content)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error_code": "storage_write_failed", "message": str(exc)},
            ) from exc

        doc_id = store.create_document(
            title=title,
            source_uri=tmp_path,
            mime_type=declared_mime,
            metadata=metadata,
            acl_tags=acl_tags,
            version=version,
        )
        # 用 Path 强制绝对路径，避免 raw_dir 相对时 os.path.join 拼出相对路径
        from pathlib import Path as _Path

        final_path = str((_Path(raw_dir) / f"{doc_id}.{ext}").resolve())
        try:
            os.replace(tmp_path, final_path)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error_code": "storage_write_failed", "message": str(exc)},
            ) from exc
        store.set_source_uri(doc_id, final_path)

        trace_id = f"trace_{doc_id}_upload"
        result = worker_client.parse(
            doc_id=doc_id,
            file_path=final_path,
            mime_type=declared_mime,
            metadata=metadata,
        )
        if result.dispatched and result.response is not None:
            store.transition_status(doc_id, DocumentStatus.PARSING.value)
            _apply_parse_response(store, doc_id, result.response)
            doc = store.get_document(doc_id)
            return _document_response(doc, trace_id, "accepted")
        if result.dispatch_status == "worker_error":
            store.transition_status(doc_id, DocumentStatus.PARSING.value)
            store.mark_parse_failed(doc_id, result.error or "worker_error", "parse")
            doc = store.get_document(doc_id)
            return _document_response(doc, trace_id, "worker_error")
        # 未接受（unreachable / timeout）：文档保持 uploaded，保留文件供 /reparse
        doc = store.get_document(doc_id)
        return _document_response(doc, trace_id, result.dispatch_status)

    @app.get("/api/v1/documents/{doc_id}", response_model=DocumentResponse)
    def get_document(doc_id: str) -> DocumentResponse:
        doc = store.get_document(doc_id)
        return _document_response(doc, f"trace_{doc_id}_get", None)

    @app.get(
        "/api/v1/documents/{doc_id}/chunks",
        response_model=DocumentChunksResponse,
    )
    def list_document_chunks(doc_id: str) -> DocumentChunksResponse:
        store.get_document(doc_id)  # 404 if missing
        chunks = store.list_chunks(doc_id)
        return DocumentChunksResponse(
            doc_id=doc_id,
            chunks=[
                ChunkResponse(
                    chunk_id=c["chunk_id"],
                    content=c["content"],
                    page_no=c["page_no"],
                    section_path=c["section_path"],
                )
                for c in chunks
            ],
        )

    @app.post("/api/v1/documents/{doc_id}/publish", response_model=DocumentResponse)
    def publish_document(doc_id: str) -> DocumentResponse:
        doc = store.get_document(doc_id)
        if doc["parse_status"] != DocumentStatus.READY.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "not_ready",
                    "current_status": doc["parse_status"],
                },
            )
        updated = store.transition_status(doc_id, DocumentStatus.PUBLISHED.value)
        return _document_response(updated, f"trace_{doc_id}_publish", None)

    @app.post("/api/v1/documents/{doc_id}/reparse", response_model=DocumentResponse)
    def reparse_document(doc_id: str) -> DocumentResponse:
        doc = store.get_document(doc_id)
        if doc["parse_status"] != DocumentStatus.PARSE_FAILED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "not_parse_failed",
                    "current_status": doc["parse_status"],
                },
            )
        store.transition_status(doc_id, DocumentStatus.UPLOADED.value)
        result = worker_client.parse(
            doc_id=doc_id,
            file_path=doc["source_uri"],
            mime_type=doc["mime_type"],
            metadata=doc["metadata"],
        )
        if result.dispatched and result.response is not None:
            store.transition_status(doc_id, DocumentStatus.PARSING.value)
            _apply_parse_response(store, doc_id, result.response)
        elif result.dispatch_status == "worker_error":
            store.transition_status(doc_id, DocumentStatus.PARSING.value)
            store.mark_parse_failed(doc_id, result.error or "worker_error", "parse")
        else:
            store.transition_status(doc_id, DocumentStatus.PARSING.value)
            store.mark_parse_failed(doc_id, result.error or "worker_unreachable", "dispatch")
        updated = store.get_document(doc_id)
        return _document_response(updated, f"trace_{doc_id}_reparse", result.dispatch_status)

    @app.post("/api/v1/documents/{doc_id}/archive", response_model=DocumentResponse)
    def archive_document(doc_id: str) -> DocumentResponse:
        updated = store.transition_status(doc_id, DocumentStatus.ARCHIVED.value)
        return _document_response(updated, f"trace_{doc_id}_archive", None)

    @app.post("/api/v1/documents/{doc_id}/restore", response_model=DocumentResponse)
    def restore_document(doc_id: str) -> DocumentResponse:
        updated = store.transition_status(doc_id, DocumentStatus.PUBLISHED.value)
        return _document_response(updated, f"trace_{doc_id}_restore", None)

    @app.post("/api/v1/chat/query", response_model=QueryResponse)
    def query(payload: QueryRequest) -> QueryResponse:
        hits = retrieval.search(
            payload.question,
            payload.knowledge_scopes,
            scope_or=payload.knowledge_scopes_or,
        )
        top = hits[0]
        trace_id = f"trace_{payload.session_id}_query"

        if _LLM_GATEWAY_ENABLED:
            from ai_employee.llm_gateway.client import LlmClient, LlmClientError
            from ai_employee.llm_gateway.prompt import RAG_ANSWER_TEMPLATE

            evidence_parts: list[str] = []
            for idx, h in enumerate(hits, start=1):
                evidence_parts.append(f"[{idx}] (《{h.doc_title}》) {h.content}")
            evidence = "\n\n".join(evidence_parts)

            prompts = RAG_ANSWER_TEMPLATE.to_messages(
                evidence=evidence,
                question=payload.question,
            )
            try:
                client = LlmClient()
                response = client.chat(prompts)
                answer = response.content
                model_name = response.model
                prompt_version = "rag-template-v1"
            except LlmClientError:
                answer = (
                    f"根据《{top.doc_title}》，{top.content} "
                    "该回答基于已发布知识片段生成，需结合现场数据人工确认。"
                )
                model_name = "template-v1-fallback"
                prompt_version = "m1-template"
        else:
            answer = (
                f"根据《{top.doc_title}》，{top.content} "
                "该回答基于已发布知识片段生成，需结合现场数据人工确认。"
            )
            model_name = "template-v1"
            prompt_version = "m1-template"

        try:
            store.write_qa_log(
                qa_log_id=trace_id.replace("trace_", "qa_"),
                session_id=payload.session_id,
                question=payload.question,
                knowledge_scopes=payload.knowledge_scopes,
                retrieved_chunks=[{"chunk_id": h.chunk_id, "doc_id": h.doc_id} for h in hits],
                answer=answer,
                model_name=model_name,
                prompt_version=prompt_version,
                confidence=top.confidence,
                latency_ms=0,
                trace_id=trace_id,
            )
        except Exception:
            # qa_log 写入失败不应阻断回答（trace_id 唯一约束冲突等）
            pass
        return QueryResponse(
            answer=answer,
            citations=[
                Citation(
                    chunk_id=h.chunk_id,
                    doc_id=h.doc_id,
                    doc_title=h.doc_title,
                    page_no=h.page_no,
                    section_path=h.section_path,
                )
                for h in hits
            ],
            confidence=top.confidence,
            trace_id=trace_id,
        )

    @app.post(
        "/api/v1/feedback",
        response_model=FeedbackResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_feedback(payload: FeedbackCreate) -> FeedbackResponse:
        feedback_id = store.write_feedback(
            trace_id=payload.trace_id,
            feedback_type=payload.feedback_type,
            comment=payload.comment,
        )
        return FeedbackResponse(
            feedback_id=feedback_id,
            trace_id=payload.trace_id,
            feedback_type=payload.feedback_type,
        )

    @app.post("/internal/chunks")
    def internal_chunks(payload: InternalChunksRequest, _: None = Depends(auth)) -> dict:
        store.write_chunks(
            doc_id=payload.doc_id,
            chunks=[c.model_dump() if hasattr(c, "model_dump") else c for c in payload.chunks],
            embeddings=payload.embeddings,
            embedding_model=payload.embedding_model,
        )
        return {"doc_id": payload.doc_id, "status": "ready"}

    @app.post("/internal/documents/{doc_id}/parse-failed")
    def internal_parse_failed(
        doc_id: str, payload: InternalParseFailedRequest, _: None = Depends(auth)
    ) -> dict:
        store.mark_parse_failed(doc_id, payload.parse_error, payload.stage)
        return {"doc_id": doc_id, "status": "parse_failed"}

    # ===== M2.1 审计端点（只读）=====

    @app.get("/api/v1/qa-logs", response_model=QaLogListResponse)
    def list_qa_logs(
        session_id: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> QaLogListResponse:
        items, total = store.list_qa_logs(
            session_id=session_id,
            user_id=user_id,
            since=since,
            until=until,
            page=page,
            page_size=page_size,
        )
        return QaLogListResponse(
            items=[QaLogSummary(**i) for i in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/qa-logs/{trace_id}", response_model=QaLogResponse)
    def get_qa_log_endpoint(trace_id: str) -> QaLogResponse:
        log = store.get_qa_log(trace_id)
        if log is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "qa_log_not_found", "trace_id": trace_id},
            )
        return QaLogResponse(**log)

    @app.get("/api/v1/feedbacks", response_model=FeedbackListResponse)
    def list_feedbacks(
        trace_id: str | None = None,
        feedback_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> FeedbackListResponse:
        items, total = store.list_feedbacks(
            trace_id=trace_id,
            feedback_type=feedback_type,
            since=since,
            until=until,
            page=page,
            page_size=page_size,
        )
        return FeedbackListResponse(
            items=[FeedbackSummary(**i) for i in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/documents", response_model=DocumentListResponse)
    def list_documents(
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DocumentListResponse:
        items, total = store.list_documents(
            status=status,
            page=page,
            page_size=page_size,
        )
        return DocumentListResponse(
            items=[DocumentSummary(**i) for i in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    return app


def _apply_parse_response(store: SQLiteStore, doc_id: str, response: Any) -> None:
    chunks = [c.model_dump() if hasattr(c, "model_dump") else c for c in response.chunks]
    store.write_chunks(
        doc_id=doc_id,
        chunks=chunks,
        embeddings=response.embeddings,
        embedding_model=response.embedding_model or "stub",
    )


def _document_response(doc: dict, trace_id: str, worker_dispatch: str | None) -> DocumentResponse:
    return DocumentResponse(
        doc_id=doc["doc_id"],
        title=doc["title"],
        mime_type=doc["mime_type"],
        parse_status=doc["parse_status"],
        parse_error=doc["parse_error"],
        chunk_count=doc["chunk_count"],
        version=doc["version"],
        trace_id=trace_id,
        metadata=doc["metadata"],
        acl_tags=doc["acl_tags"],
        worker_dispatch=worker_dispatch,
        updated_at=doc["updated_at"],
    )


app = create_app()
