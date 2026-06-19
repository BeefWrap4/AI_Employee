from __future__ import annotations

import os
from pathlib import Path

from ai_employee.common_schemas.embedding import build_provider
from ai_employee.common_schemas.knowledge import (
    ParseRequest,
    ParseResponse,
)
from ai_employee.common_schemas.security import (
    UnsafeSourceUriError,
    assert_safe_source_uri,
)
from ai_employee.common_schemas.sparse_store import (
    OpenSearchSparseStore,
    StubSparseStore,
)
from ai_employee.common_schemas.vector_store import (
    VectorStore,
    build_vector_store,
)
from ai_employee.ingestion_worker.chunker import chunk_sections
from ai_employee.ingestion_worker.embedding import EmbeddingProvider
from ai_employee.ingestion_worker.parsers import get_parser
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

SERVICE_VERSION = "0.1.0"

_BINARY_MIME = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def create_app(
    provider: EmbeddingProvider | None = None,
    degraded: bool | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Ingestion Worker", version=SERVICE_VERSION)
    # R25-L: shared rate-limit middleware (no-op unless RATE_LIMIT_ENABLED=true).
    from ai_employee.rate_limit import install_rate_limiter

    install_rate_limiter(app)
    if provider is None:
        embed_provider, built_degraded = build_provider()
        embed_degraded = built_degraded
    else:
        embed_provider = provider
        embed_degraded = bool(degraded) if degraded is not None else False
    state = {"last_call_ok": True}

    import logging

    logger = logging.getLogger(__name__)

    # Initialize sparse (BM25) store for the full-text search pipeline.
    opensearch_enabled = os.getenv("OPENSEARCH_ENABLED", "false").strip().lower() == "true"
    if opensearch_enabled:
        sparse_store: OpenSearchSparseStore | StubSparseStore = OpenSearchSparseStore()
        sparse_store.create_index("knowledge_base")
    else:
        sparse_store = StubSparseStore()

    # Initialize vector store (Milvus or stub fallback).
    vector_store: VectorStore = build_vector_store()

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "service": "ingestion-worker",
            "status": "ok",
            "version": SERVICE_VERSION,
            "embedding_provider": embed_provider.name,
            "embedding_provider_degraded": embed_degraded,
            "last_call_ok": state["last_call_ok"],
        }

    @app.post("/internal/parse", response_model=ParseResponse)
    def parse(request: ParseRequest) -> ParseResponse:
        # 路径校验：必须位于 ${KNOWLEDGE_DATA_DIR}/raw/ 之下
        data_dir = os.getenv("KNOWLEDGE_DATA_DIR", "./var/data")
        try:
            assert_safe_source_uri(request.file_path, data_dir)
        except UnsafeSourceUriError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "path_not_allowed", "message": str(exc)},
            ) from exc
        path = Path(request.file_path)
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "file_not_found", "file_path": request.file_path},
            )

        parser = get_parser(request.mime_type)
        if type(parser).__name__ == "NotImplementedParser":
            return JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content={
                    "error_code": "mime_unsupported",
                    "mime_type": request.mime_type,
                },
            )

        # Binary file types: read as bytes; text types: read as string
        if request.mime_type in _BINARY_MIME:
            try:
                source = path.read_bytes()
            except OSError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error_code": "file_read_error", "message": str(exc)},
                ) from exc
        else:
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error_code": "file_read_error", "message": str(exc)},
                ) from exc
        try:
            sections = parser.parse(source)
        except NotImplementedError as exc:
            return JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content={
                    "error_code": "mime_unsupported",
                    "mime_type": request.mime_type,
                    "message": str(exc),
                },
            )

        parsed_chunks = chunk_sections(request.doc_id, sections)
        if not parsed_chunks:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error_code": "empty_content",
                    "doc_id": request.doc_id,
                },
            )

        try:
            embeddings = embed_provider.embed([c.content for c in parsed_chunks])
            state["last_call_ok"] = True
        except Exception as exc:
            state["last_call_ok"] = False
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error_code": "embed_unavailable", "message": str(exc)},
            ) from exc
        if len(embeddings) != len(parsed_chunks):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "embed_count_mismatch",
                    "expected": len(parsed_chunks),
                    "got": len(embeddings),
                },
            )

        for chunk, vec in zip(parsed_chunks, embeddings, strict=False):
            chunk.embedding = vec

        # Also index chunks into the sparse (BM25) store for full-text search.
        # This is a best-effort side effect -- failures are logged, not raised.
        documents = [
            {
                "chunk_id": c.chunk_id,
                "doc_id": request.doc_id,
                "content": c.content,
                "section_path": c.section_path,
            }
            for c in parsed_chunks
        ]
        try:
            sparse_store.bulk_index("knowledge_base", documents)
        except Exception:
            pass  # best-effort; logged inside the store

        # Also write vectors to Milvus (or stub) for ANN vector recall.
        # Best-effort side effect -- failures are logged, not raised, to keep
        # the existing SQLite write path intact.
        try:
            vector_store.create_collection("chunks", len(embeddings[0]))
            vector_store.insert(
                "chunks",
                vectors=embeddings,
                metadata=[
                    {
                        "chunk_id": c.chunk_id,
                        "doc_id": request.doc_id,
                        "chunk_no": c.chunk_no,
                        "content": c.content,
                        "section_path": c.section_path,
                        "page_no": c.page_no,
                        "embedding_model": embed_provider.name,
                    }
                    for c in parsed_chunks
                ],
            )
        except Exception as exc:
            logger.warning("Vector store insert failed (best-effort): %s", exc)

        return ParseResponse(
            doc_id=request.doc_id,
            chunks=parsed_chunks,
            embeddings=embeddings,
            embedding_model=embed_provider.name,
        )

    return app


app = create_app()
