from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from ai_employee.common_schemas.embedding import build_provider
from ai_employee.common_schemas.knowledge import (
    ParseRequest,
    ParseResponse,
)
from ai_employee.ingestion_worker.chunker import chunk_sections
from ai_employee.ingestion_worker.embedding import EmbeddingProvider
from ai_employee.ingestion_worker.parsers import get_parser


SERVICE_VERSION = "0.1.0"


def create_app(
    provider: EmbeddingProvider | None = None,
    degraded: bool | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Ingestion Worker", version=SERVICE_VERSION)
    if provider is None:
        embed_provider, built_degraded = build_provider()
        embed_degraded = built_degraded
    else:
        embed_provider = provider
        embed_degraded = bool(degraded) if degraded is not None else False
    state = {"last_call_ok": True}

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
        path = Path(request.file_path)
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "file_not_found", "file_path": request.file_path},
            )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "file_read_error", "message": str(exc)},
            ) from exc

        parser = get_parser(request.mime_type)
        if type(parser).__name__ == "NotImplementedParser":
            return JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content={
                    "error_code": "mime_unsupported",
                    "mime_type": request.mime_type,
                },
            )
        try:
            sections = parser.parse(text)
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

        for chunk, vec in zip(parsed_chunks, embeddings):
            chunk.embedding = vec

        return ParseResponse(
            doc_id=request.doc_id,
            chunks=parsed_chunks,
            embeddings=embeddings,
            embedding_model=embed_provider.name,
        )

    return app


app = create_app()
