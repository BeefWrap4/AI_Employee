from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from typing import Any

from ai_employee.auth_policy.fastapi_dep import (
    OIDCOrInternalPrincipal,
    require_oidc_or_internal,
)
from ai_employee.common_schemas.embedding import build_provider
from ai_employee.common_schemas.idempotency import (
    IdempotencyStore,
    build_idempotency_store,
)
from ai_employee.common_schemas.knowledge import DocumentStatus
from ai_employee.common_schemas.metrics_bridge import platform_metrics
from ai_employee.knowledge_api.internal_auth import require_internal_token
from ai_employee.knowledge_api.retrieval import RetrievalService
from ai_employee.knowledge_api.schemas import (
    ChunkResponse,
    Citation,
    DocumentChunksResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentSummary,
    EChartsRequest,
    EChartsResponse,
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
    Request,
    UploadFile,
    status,
)


# --------------------------------------------------------------------------- #
# R19-2 default aggregator stubs (alarm/KPI backends are pluggable).
# --------------------------------------------------------------------------- #
class _StubAlarmAggregator:
    """Default alarm aggregator: returns empty (no data → 404)."""

    def bucket_alarms(self, *, metric, window_minutes, now):  # type: ignore[no-untyped-def]
        return []


class _StubKpiAggregator:
    """Default KPI aggregator: returns empty (no data → 404)."""

    def bucket_kpi(self, *, metric, site_id, window_minutes, now):  # type: ignore[no-untyped-def]
        return []


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
    idempotency_store: IdempotencyStore | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Knowledge API", version=SERVICE_VERSION)
    # R25-L: shared rate-limit middleware (no-op unless RATE_LIMIT_ENABLED=true).
    from ai_employee.rate_limit import install_rate_limiter

    install_rate_limiter(app)
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
    from ai_employee.knowledge_api.reranker import build_reranker

    retrieval = RetrievalService(store, query_provider=query_provider, reranker=build_reranker())
    # 暴露 retrieval 让测试可注入 query provider
    app.state.retrieval = retrieval
    auth = require_internal_token(cfg["internal_token"])
    # R24-A.4: production write endpoints now accept OIDC Bearer tokens,
    # the legacy HS256 JWT, or the internal-token fallback.  When OIDC
    # env vars are unset this dep degrades to ``require_internal_or_jwt``
    # behaviour so backward compatibility is preserved.  The internal
    # token is read from ``KNOWLEDGE_API_INTERNAL_TOKEN`` to keep the
    # service-specific secret isolated from the cross-service
    # ``INTERNAL_TOKEN`` used by other services.
    write_auth = require_oidc_or_internal(
        permissions=["knowledge:write"],
        internal_token_env="KNOWLEDGE_API_INTERNAL_TOKEN",
    )

    # R23: idempotency store so a retried document upload with the same
    # Idempotency-Key + content returns the cached doc_id instead of
    # creating a duplicate.  Default in-memory; set REDIS_URL for
    # multi-replica.
    if idempotency_store is not None:
        idem_store: IdempotencyStore = idempotency_store
    else:
        idem_store = build_idempotency_store()

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
        request: Request,
        file: UploadFile = File(...),
        title: str = Form(...),
        metadata_json: str = Form("{}"),
        acl_tags_json: str = Form("[]"),
        version: str = Form("v1"),
        mime_type: str | None = Form(None),
        _principal: OIDCOrInternalPrincipal = Depends(write_auth),
    ) -> DocumentResponse:
        content = await file.read()
        # R23: idempotency.  The cache key is the Idempotency-Key header
        # + a sha256 of the uploaded bytes, so the same key with
        # different content still creates a new doc (avoids masking a
        # genuine new upload behind a stale cache entry).
        idem_raw = request.headers.get("Idempotency-Key")
        idem_key: str | None = None
        if idem_raw is not None and idem_raw.strip():
            content_hash = hashlib.sha256(content).hexdigest()
            idem_key = f"{idem_raw.strip()}:{content_hash}"
            rec = idem_store.get_or_begin(idem_key)
            if rec.status in {"success", "failed"} and rec.result is not None:
                return DocumentResponse(**rec.result["body"])
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

        # R22: remember the object store key alongside the document so
        # the upload-to-publish flow can retrieve the bytes from S3 /
        # MinIO without re-uploading.  This is metadata-only — the
        # on-disk path below is still required for the ingestion worker.
        obj_key: str | None = None

        ext = _MIME_EXT[declared_mime]
        raw_dir = os.path.join(store.data_dir, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        # R22: write-through to the configured object store.  When
        # OBJECT_STORE_URL is unset (LocalFs default in dev/test) this
        # is a no-op against ``./var/objects``; in production the same
        # code writes to S3 / MinIO.  Local-disk write below stays so
        # the ingestion worker can still read the file by path
        # (backward compat).
        try:
            from ai_employee.object_store import build_object_store

            _store = build_object_store()
            obj_key = f"documents/{uuid.uuid4().hex}.{ext}"
            _store.put(
                obj_key,
                content,
                content_type=declared_mime,
                # S3 user-metadata is sent as HTTP headers and must be ASCII;
                # the Unicode title is preserved in the DB row below, this
                # header is only a best-effort label for the object.
                metadata={"title": title.encode("ascii", "replace").decode("ascii")},
            )
        except Exception as exc:  # pragma: no cover - storage is best-effort
            obj_key = None
            import logging

            logging.getLogger(__name__).warning(
                "object_store write failed; continuing with local-only upload: %s",
                exc,
            )
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
            metadata={**metadata, "object_key": obj_key} if obj_key else metadata,
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
            response = _document_response(doc, trace_id, "accepted")
        elif result.dispatch_status == "worker_error":
            store.transition_status(doc_id, DocumentStatus.PARSING.value)
            store.mark_parse_failed(doc_id, result.error or "worker_error", "parse")
            doc = store.get_document(doc_id)
            response = _document_response(doc, trace_id, "worker_error")
        else:
            # 未接受（unreachable / timeout）：文档保持 uploaded，保留文件供 /reparse
            doc = store.get_document(doc_id)
            response = _document_response(doc, trace_id, result.dispatch_status)
        if idem_key is not None:
            idem_store.complete(
                idem_key,
                status="success",
                result={"body": response.model_dump(mode="json")},
            )
        return response

    @app.get("/api/v1/documents/{doc_id}", response_model=DocumentResponse)
    def get_document(doc_id: str) -> DocumentResponse:
        doc = store.get_document(doc_id)
        return _document_response(doc, f"trace_{doc_id}_get", None)

    @app.get("/api/v1/documents/{doc_id}/upload-progress")
    async def upload_progress_stream(doc_id: str):  # type: ignore[no-untyped-def]
        """SSE stream of upload progress for ``doc_id``.

        Replays the latest snapshot then streams new progress events
        until the client disconnects or the upload completes/fails.
        Terminates after one snapshot when no progress is recorded
        yet (``stage == 'unknown'``) so unknown doc_id requests don't
        hang.
        """
        import asyncio
        import json

        from ai_employee.knowledge_api.upload_progress import build_progress_tracker
        from fastapi.responses import StreamingResponse

        tracker = build_progress_tracker()
        initial = tracker.get(doc_id)
        queue = tracker.subscribe(doc_id)

        async def event_source():
            try:
                # Emit the initial snapshot; terminate if it's a terminal
                # state (completed/failed) or unknown (no upload recorded).
                yield f"data: {json.dumps(initial.to_dict(), ensure_ascii=False)}\n\n"
                if initial.stage in {"completed", "failed", "unknown"}:
                    return
                while True:
                    progress = await queue.get()
                    yield f"data: {json.dumps(progress.to_dict(), ensure_ascii=False)}\n\n"
                    if progress.stage in {"completed", "failed"}:
                        return
            except asyncio.CancelledError:
                raise
            finally:
                tracker.unsubscribe(doc_id=doc_id, queue=queue)

        return StreamingResponse(event_source(), media_type="text/event-stream")

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
        return _answer_query(payload)

    @app.post("/api/v1/chat/echarts", response_model=EChartsResponse)
    def chat_echarts(payload: EChartsRequest) -> EChartsResponse:
        """Return an ECharts option dict for the requested trend metric.

        The endpoint composes alarm + KPI aggregators (R19-2).  Production
        wiring reads from rca-agent ``AlarmEvent`` + InfluxDB KPI; tests
        inject a custom aggregator via ``app.state.echarts_aggregator``.
        """
        from ai_employee.knowledge_api.echarts import (
            EChartsAggregator,
            InfluxKpiAggregator,
            RcaAgentAlarmAggregator,
        )

        aggregator = getattr(app.state, "echarts_aggregator", None)
        if aggregator is None:
            # Lazy default wiring: try to load rca-agent store + KPI adapter.
            alarm_agg: Any = _StubAlarmAggregator()
            kpi_agg: Any = _StubKpiAggregator()
            try:
                from ai_employee.rca_agent.store import RcaObjectStore

                rca = RcaObjectStore()
                rca.init_schema()
                rca.load()
                alarm_agg = RcaAgentAlarmAggregator(rca)
            except Exception:
                pass
            try:
                from ai_employee.rca_agent.kpi_influx import build_influx_kpi_adapter

                adapter = build_influx_kpi_adapter()
                if adapter is not None:
                    kpi_agg = InfluxKpiAggregator(adapter)
            except Exception:
                pass
            aggregator = EChartsAggregator(alarm=alarm_agg, kpi=kpi_agg)

        from datetime import datetime as _dt
        from datetime import timezone as _tz

        now = _dt.now(_tz.utc)
        option = aggregator.build_option(
            metric=payload.metric,
            site_id=payload.site_id,
            window_minutes=payload.window_minutes,
            now=now,
        )
        if not option:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "no_trend_data", "metric": payload.metric},
            )
        # chart_id is a stable ref the SSE stream can echo back.
        import uuid

        chart_id = f"chart_{uuid.uuid4().hex[:12]}"
        # Persist option so the schema lookup endpoint can resolve it later.
        store_map: dict[str, dict[str, Any]] = getattr(app.state, "echarts_chart_store", {})
        store_map[chart_id] = option
        app.state.echarts_chart_store = store_map
        return EChartsResponse(
            metric=payload.metric,
            site_id=payload.site_id,
            window_minutes=payload.window_minutes,
            xAxis=option["xAxis"],
            yAxis=option["yAxis"],
            series=option["series"],
            chart_id=chart_id,
            schema_url=f"/api/v1/chat/echarts/schema/{chart_id}",
        )

    @app.get("/api/v1/chat/echarts/schema/{chart_id}")
    def get_echarts_schema(chart_id: str) -> dict[str, Any]:
        """Resolve a chart_id back to its ECharts option dict.

        The endpoint is used by the SSE ``chart`` event reference: the
        stream emits only ``chart_id + schema_url``; the consumer fetches
        the full option lazily via this endpoint.
        """
        store_map: dict[str, dict[str, Any]] = getattr(app.state, "echarts_chart_store", {})
        option = store_map.get(chart_id)
        if option is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "chart_not_found", "chart_id": chart_id},
            )
        return {"chart_id": chart_id, **option}

    @app.post("/api/v1/chat/query/stream")
    def query_stream(payload: QueryRequest):
        """Server-Sent Events streaming answer (spec §1: stream=true).

        Emits one ``event: token\\ndata: ...\\n\\n`` chunk per token of the
        final answer, followed by a ``done`` event with the trace_id and
        a ``citations`` event with the structured evidence.  Sessions are
        resolved through the qa_log store (session_id ↔ prior turns) so
        follow-up questions can refer to previous context implicitly.

        When the request implies a trend (alarm/KPI metric keyword in the
        question) the stream also emits ``event: chart`` with a
        ``chart_id`` + ``schema_url`` reference (R19-3).  Consumers can
        resolve the chart lazily via ``GET <schema_url>`` to avoid
        streaming a large option dict inline.
        """
        from fastapi.responses import StreamingResponse

        result = _answer_query(payload)

        # Optional: derive a chart reference if the question hints at a trend.
        chart_ref = _maybe_build_chart_ref(app=app, payload=payload)

        def _gen():
            yield f"event: meta\ndata: {json.dumps({'trace_id': result.trace_id})}\n\n"
            # Stream the answer in ~40-char chunks so the consumer sees
            # incremental progress without trying to be token-perfect.
            text = result.answer
            for start in range(0, len(text), 40):
                yield f"event: token\ndata: {json.dumps({'text': text[start : start + 40]})}\n\n"
            citations_payload = [c.model_dump() for c in result.citations]
            yield f"event: citations\ndata: {json.dumps({'citations': citations_payload})}\n\n"
            if chart_ref is not None:
                yield (f"event: chart\ndata: {json.dumps(chart_ref, ensure_ascii=False)}\n\n")
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.get("/api/v1/chat/sessions/{session_id}/history")
    def session_history(session_id: str) -> dict[str, Any]:
        """Return the multi-turn conversation history for a session.

        Powers the ``multi-turn`` question flow (spec §5.5: 支持多轮追问):
        follow-up questions can reference prior turns by reusing retrieved
        chunks and prior answers.
        """
        rows, total = store.list_qa_logs(session_id=session_id)
        return {
            "session_id": session_id,
            "turns": [
                {
                    "qa_log_id": r.get("qa_log_id"),
                    "question": r.get("question"),
                    "answer": r.get("answer"),
                    "trace_id": r.get("trace_id"),
                    "created_at": r.get("created_at"),
                }
                for r in rows
            ],
            "total": total,
        }

    def _answer_query(payload: QueryRequest) -> QueryResponse:
        # Multi-turn context: collect this session's prior retrieved chunks +
        # answers and stitch them into a ``context_str`` block.  This block is
        # injected into the LLM prompt so follow-up questions can resolve
        # pronouns / references against earlier turns (R19-1).
        prior_rows, _ = store.list_qa_logs(session_id=payload.session_id)
        # Hydrate prior rows with retrieved_chunks via get_qa_log so the
        # context string can reference the actual prior chunks.
        hydrated: list[dict[str, Any]] = []
        for row in prior_rows:
            trace_id = row.get("trace_id")
            if trace_id:
                full = store.get_qa_log(trace_id)
                if full is not None:
                    hydrated.append(full)
                    continue
            hydrated.append(row)
        context_str = _build_multiturn_context_str(store=store, prior_rows=hydrated)
        prior_turn_hint = ""
        if context_str:
            prior_turn_hint = (
                f"\n\n[上轮回答]\n{prior_rows[-1].get('answer', '')[:200]}\n"
                f"[当前问题]\n{payload.question}"
            )
        effective_question = (
            payload.question + prior_turn_hint if prior_turn_hint else payload.question
        )
        hits = retrieval.search(
            effective_question,
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
                context_str=context_str,
            )
            try:
                # R24-B: bare LlmClient() picks up the default Langfuse
                # emitter from env (LANGFUSE_ENABLED).  Pass
                # ``parent_trace_id`` so the answer-completion chat and
                # the query-rewriter chat (when used upstream) share
                # the same trace.
                client = LlmClient(
                    on_success=lambda latency_ms: platform_metrics().record_model_latency(latency_ms),
                )
                response = client.chat(prompts, parent_trace_id=trace_id)
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
            # Sensitive field redaction (spec §8): mask PII before
            # persisting QA log so reviewers don't see raw phone / email
            # / ID numbers in audit trails.
            from ai_employee.common_schemas.redaction import redact_text

            redacted_question = redact_text(payload.question)
            redacted_answer = redact_text(answer)
            store.write_qa_log(
                qa_log_id=trace_id.replace("trace_", "qa_"),
                session_id=payload.session_id,
                question=redacted_question,
                knowledge_scopes=payload.knowledge_scopes,
                retrieved_chunks=[{"chunk_id": h.chunk_id, "doc_id": h.doc_id} for h in hits],
                answer=redacted_answer,
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


def _maybe_build_chart_ref(*, app: FastAPI, payload: QueryRequest) -> dict[str, str] | None:
    """Return ``{chart_id, schema_url}`` if the question implies a trend.

    The question is scanned for alarm/KPI keywords (Chinese + English).  If
    a metric is detected, we attempt to build a chart via the configured
    aggregator (or the default one).  Returns ``None`` when no aggregator
    yields data so the SSE stream can simply omit the chart event.
    """
    question = payload.question or ""
    metric = _detect_trend_metric(question)
    if metric is None:
        return None
    # Reuse the same wiring logic as the REST endpoint.
    from ai_employee.knowledge_api.echarts import (
        EChartsAggregator,
        InfluxKpiAggregator,
        RcaAgentAlarmAggregator,
    )

    aggregator = getattr(app.state, "echarts_aggregator", None)
    if aggregator is None:
        alarm_agg: Any = _StubAlarmAggregator()
        kpi_agg: Any = _StubKpiAggregator()
        try:
            from ai_employee.rca_agent.store import RcaObjectStore

            rca = RcaObjectStore()
            rca.init_schema()
            rca.load()
            alarm_agg = RcaAgentAlarmAggregator(rca)
        except Exception:
            pass
        try:
            from ai_employee.rca_agent.kpi_influx import build_influx_kpi_adapter

            adapter = build_influx_kpi_adapter()
            if adapter is not None:
                kpi_agg = InfluxKpiAggregator(adapter)
        except Exception:
            pass
        aggregator = EChartsAggregator(alarm=alarm_agg, kpi=kpi_agg)
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    option = aggregator.build_option(
        metric=metric,
        site_id=None,
        window_minutes=60,
        now=_dt.now(_tz.utc),
    )
    if not option:
        return None
    import uuid

    chart_id = f"chart_{uuid.uuid4().hex[:12]}"
    store_map: dict[str, dict[str, Any]] = getattr(app.state, "echarts_chart_store", {})
    store_map[chart_id] = option
    app.state.echarts_chart_store = store_map
    return {
        "chart_id": chart_id,
        "schema_url": f"/api/v1/chat/echarts/schema/{chart_id}",
    }


_TREND_KEYWORDS = {
    # keyword → metric
    "告警": "alarm_count",
    "alarm": "alarm_count",
    "趋势": "alarm_count",
    "trend": "alarm_count",
}


def _detect_trend_metric(question: str) -> str | None:
    lower = question.lower()
    for kw, metric in _TREND_KEYWORDS.items():
        if kw in lower or kw.lower() in lower:
            return metric
    return None


def _build_multiturn_context_str(*, store: SQLiteStore, prior_rows: list[dict[str, Any]]) -> str:
    """Build the multi-turn context string from prior qa_log rows.

    Each prior turn contributes:
      - the prior question,
      - the prior answer,
      - the retrieved chunks (chunk_id + doc_id + content).

    Returns an empty string when no prior turns exist so callers can detect
    "first turn" and skip the context block.
    """
    if not prior_rows:
        return ""
    parts: list[str] = []
    for row in prior_rows:
        q = row.get("question") or ""
        a = row.get("answer") or ""
        chunks = row.get("retrieved_chunks") or []
        chunk_lines: list[str] = []
        for c in chunks:
            cid = c.get("chunk_id", "")
            did = c.get("doc_id", "")
            content = ""
            try:
                fetched = store.get_chunk(cid) if cid else None
                if fetched:
                    content = fetched.get("content", "") or ""
            except Exception:
                # Skip chunks that can't be fetched (e.g. doc was deleted)
                content = ""
            line = f"  - chunk_id={cid} doc_id={did}"
            if content:
                line += f" content={content[:200]}"
            chunk_lines.append(line)
        block = (
            f"[历史上下文] session_turn trace_id={row.get('trace_id', '')}\n"
            f"  question: {q[:200]}\n"
            f"  answer: {a[:200]}\n"
            f"  retrieved_chunks:\n" + "\n".join(chunk_lines)
        )
        parts.append(block)
    return "\n\n".join(parts)


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
