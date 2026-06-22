from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from ai_employee.auth_policy.fastapi_dep import (
    OIDCOrInternalPrincipal,
    require_oidc_or_internal,
)
from ai_employee.rca_agent.knowledge_feedback import (
    KnowledgeApiError,
    KnowledgeApiUnavailable,
    generate_candidates_from_report,
    import_candidate_to_knowledge_api,
)
from ai_employee.rca_agent.metrics import compute_metrics
from ai_employee.rca_agent.runtime import (
    RcaStore,
    build_incident,
    normalize_alarm,
    resume_with_more_evidence,
    run_rca,
)
from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    CandidateKnowledge,
    CandidateListResponse,
    CandidateReviewRequest,
    CandidateReviewResponse,
    IncidentBuildRequest,
    IncidentResponse,
    RawAlarmEvent,
    RcaReportListResponse,
    RcaReportResponse,
    RcaReportSummary,
    RcaRunCreate,
    RcaRunListResponse,
    RcaRunResponse,
    RcaRunSummary,
    ReportReviewRequest,
    ReportReviewResponse,
    TicketWritebackRequest,
    TicketWritebackResponse,
)
from ai_employee.rca_agent.store import SQLiteRcaStore
from ai_employee.rca_agent.ticket_writeback import (
    TicketWritebackError,
    TicketWritebackStore,
    TicketWritebackUnavailable,
    build_writeback_adapter,
)
from fastapi import Depends, FastAPI, HTTPException, status

SERVICE_VERSION = "0.1.0"

_LOG = logging.getLogger(__name__)


def create_app(store: RcaStore | None = None) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # R29-C: Kafka alarm ingestion moved to the event-gateway
        # service.  rca-agent is now a pure HTTP consumer of
        # ``POST /api/v1/alarms/events``; no embedded consumer here.
        yield

    app = FastAPI(
        title="AI Employee RCA Agent",
        version=SERVICE_VERSION,
        lifespan=_lifespan,
    )
    # R25-L: shared rate-limit middleware (no-op unless RATE_LIMIT_ENABLED=true).
    from ai_employee.rate_limit import install_rate_limiter

    install_rate_limiter(app)
    state = store or _default_store()
    # R29-A: log which backend we actually wired so operators can
    # confirm the runtime choice from ``kubectl logs``.
    from ai_employee.common_schemas.db import detect_backend

    backend_label = (
        "postgresql"
        if detect_backend(os.getenv("DATABASE_URL", "")).name.lower() == "postgres"
        else "sqlite"
    )
    _LOG.info(
        "rca-agent create_app: using %s:// storage backend",
        backend_label,
    )
    if state.writeback_adapter is None:
        state.writeback_adapter = build_writeback_adapter()
    if state.writebacks is None:
        state.writebacks = TicketWritebackStore()

    # R24-A.4: production write endpoints now require authentication via
    # OIDC (RS256) when SSO is enabled, the legacy HS256 JWT, or the
    # ``X-Internal-Token`` shared secret.  Each dependency tier uses
    # the appropriate RBAC permission so OIDC/JWT principals are
    # permission-checked while internal service callers (legacy
    # telemetry / write-back adapters) remain trusted.
    write_auth = require_oidc_or_internal(permissions=["rca:write"])
    review_auth = require_oidc_or_internal(permissions=["rca:approve"])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "rca-agent",
            "status": "ok",
            "version": SERVICE_VERSION,
            "runtime": "sqlite_dag" if isinstance(state, SQLiteRcaStore) else "in_memory_dag",
        }

    @app.post(
        "/api/v1/alarms/events",
        response_model=AlarmEvent,
        status_code=status.HTTP_201_CREATED,
    )
    def create_alarm_event(
        payload: RawAlarmEvent,
        _principal: OIDCOrInternalPrincipal = Depends(write_auth),
    ) -> AlarmEvent:
        return normalize_alarm(state, payload)

    @app.post(
        "/api/v1/incidents/build",
        response_model=IncidentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_incident(
        payload: IncidentBuildRequest,
        _principal: OIDCOrInternalPrincipal = Depends(write_auth),
    ) -> IncidentResponse:
        return build_incident(
            state,
            payload.alarms,
            payload.time_window_minutes,
            topology_window_minutes=payload.topology_window_minutes,
            parent_child_lag_seconds=payload.parent_child_lag_seconds,
            topology_client=getattr(state, "topology_client", None),
        )

    @app.post(
        "/api/v1/rca/runs",
        response_model=RcaRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_rca_run(
        payload: RcaRunCreate,
        _principal: OIDCOrInternalPrincipal = Depends(write_auth),
    ) -> RcaRunResponse:
        if not payload.incident_id and not payload.alarms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "incident_or_alarms_required"},
            )
        if (
            payload.incident_id
            and payload.incident_id not in state.incidents
            and not payload.alarms
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "incident_not_found", "incident_id": payload.incident_id},
            )
        return run_rca(
            state,
            raw_alarms=payload.alarms,
            incident_id=payload.incident_id,
            require_human_review=payload.require_human_review,
        )

    @app.get("/api/v1/rca/runs", response_model=RcaRunListResponse)
    def list_rca_runs(
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> RcaRunListResponse:
        runs = list(state.runs.values())
        if status is not None:
            runs = [run for run in runs if run.status == status]
        total = len(runs)
        page, page_size, start, end = _page_bounds(page, page_size)
        return RcaRunListResponse(
            items=[
                RcaRunSummary(
                    run_id=run.run_id,
                    incident_id=run.incident_id,
                    report_id=run.report_id,
                    status=run.status,
                    current_node=run.current_node,
                    trace_id=run.trace_id,
                    evidence_count=run.evidence_count,
                    hypothesis_count=len(run.hypotheses),
                )
                for run in runs[start:end]
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/rca/runs/{run_id}", response_model=RcaRunResponse)
    def get_rca_run(run_id: str) -> RcaRunResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "rca_run_not_found", "run_id": run_id},
            )
        return run

    @app.post("/api/v1/rca/runs/{run_id}/resume", response_model=RcaRunResponse)
    def resume_rca_run(run_id: str) -> RcaRunResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "rca_run_not_found", "run_id": run_id},
            )
        if run.status != "need_more_evidence":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "run_not_waiting_for_more_evidence",
                    "current_status": run.status,
                },
            )
        return resume_with_more_evidence(state, run_id)

    @app.get("/api/v1/rca/reports", response_model=RcaReportListResponse)
    def list_rca_reports(
        review_status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> RcaReportListResponse:
        reports = list(state.reports.values())
        if review_status is not None:
            reports = [report for report in reports if report.review_status == review_status]
        total = len(reports)
        page, page_size, start, end = _page_bounds(page, page_size)
        return RcaReportListResponse(
            items=[
                RcaReportSummary(
                    report_id=report.report_id,
                    run_id=report.run_id,
                    incident_id=report.incident_id,
                    review_status=report.review_status,
                    final_root_cause=report.final_root_cause,
                    evidence_count=len(report.evidence),
                    hypothesis_count=len(report.hypotheses),
                )
                for report in reports[start:end]
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/rca/reports/{report_id}", response_model=RcaReportResponse)
    def get_rca_report(report_id: str) -> RcaReportResponse:
        report = state.reports.get(report_id)
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "rca_report_not_found", "report_id": report_id},
            )
        return report

    @app.post(
        "/api/v1/rca/reports/{report_id}/review",
        response_model=ReportReviewResponse,
    )
    def review_report(
        report_id: str,
        payload: ReportReviewRequest,
        _principal: OIDCOrInternalPrincipal = Depends(review_auth),
    ) -> ReportReviewResponse:
        report = state.reports.get(report_id)
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "rca_report_not_found", "report_id": report_id},
            )
        updated = report.model_copy(
            update={
                "review_status": payload.decision,
                "final_root_cause": payload.final_root_cause,
            }
        )
        state.reports[report_id] = updated
        # Operational metrics: count this review and the verdict.
        state.reviewed_reports += 1
        if payload.decision == "accepted":
            state.accepted_reports += 1
        elif payload.decision == "rejected":
            state.rejected_reports += 1
        run = state.runs.get(report.run_id)
        if run is not None:
            run_update = None
            if payload.decision == "need_more_evidence":
                run_update = {
                    "status": "need_more_evidence",
                    "current_node": "NeedMoreEvidence",
                    "state_history": [*run.state_history, "NeedMoreEvidence"],
                }
            elif payload.decision in {"accepted", "rejected"}:
                run_update = {
                    "status": payload.decision,
                    "current_node": "Accepted" if payload.decision == "accepted" else "Rejected",
                    "state_history": [
                        *run.state_history,
                        "Accepted" if payload.decision == "accepted" else "Rejected",
                    ],
                }
            if run_update is not None:
                run = run.model_copy(update=run_update)
                state.runs[run.run_id] = run
                if hasattr(state, "save_run"):
                    state.save_run(run)
        if hasattr(state, "save_report"):
            state.save_report(updated)
        if payload.decision == "accepted" and payload.final_root_cause:
            _generate_and_persist_candidates(state, updated)
        # Redact reviewer comments before returning (PII in comments is
        # surfaced back to clients unchanged; persistence already happens
        # in store layer).  When the comment is stored it goes through
        # writeback, which itself sanitises — see ticket_writeback.
        return ReportReviewResponse(
            report_id=report_id,
            review_status=payload.decision,
            final_root_cause=payload.final_root_cause,
            reviewer=payload.reviewer,
            comment=payload.comment,
        )

    @app.get("/api/v1/candidate-knowledge", response_model=CandidateListResponse)
    def list_candidate_knowledge(
        review_status: str | None = None,
        incident_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> CandidateListResponse:
        items = list(state.candidates.values())
        if review_status is not None:
            items = [c for c in items if c.review_status == review_status]
        if incident_id is not None:
            items = [c for c in items if c.source_incident_id == incident_id]
        items.sort(key=lambda c: c.candidate_id)
        total = len(items)
        page, page_size, start, end = _page_bounds(page, page_size)
        return CandidateListResponse(
            items=items[start:end],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get(
        "/api/v1/candidate-knowledge/{candidate_id}",
        response_model=CandidateKnowledge,
    )
    def get_candidate_knowledge(candidate_id: str) -> CandidateKnowledge:
        candidate = state.candidates.get(candidate_id)
        if candidate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "candidate_not_found", "candidate_id": candidate_id},
            )
        return candidate

    @app.post(
        "/api/v1/candidate-knowledge/{candidate_id}/review",
        response_model=CandidateReviewResponse,
    )
    def review_candidate(
        candidate_id: str,
        payload: CandidateReviewRequest,
        _principal: OIDCOrInternalPrincipal = Depends(review_auth),
    ) -> CandidateReviewResponse:
        candidate = state.candidates.get(candidate_id)
        if candidate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "candidate_not_found", "candidate_id": candidate_id},
            )
        if candidate.review_status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "already_reviewed",
                    "current_status": candidate.review_status,
                },
            )
        reviewed_at = datetime.now(timezone.utc).isoformat()
        updated = candidate.model_copy(
            update={
                "review_status": payload.decision,
                "reviewer": payload.reviewer,
                "review_comment": payload.comment,
                "reviewed_at": reviewed_at,
            }
        )
        state.candidates[candidate_id] = updated
        if hasattr(state, "save_candidate"):
            state.save_candidate(updated)
        return CandidateReviewResponse(
            candidate_id=candidate_id,
            review_status=updated.review_status,
            reviewer=updated.reviewer,
            review_comment=updated.review_comment,
            reviewed_at=updated.reviewed_at,
        )

    @app.post(
        "/api/v1/candidate-knowledge/{candidate_id}/import",
        response_model=CandidateKnowledge,
    )
    def import_candidate(
        candidate_id: str,
        _principal: OIDCOrInternalPrincipal = Depends(review_auth),
    ) -> CandidateKnowledge:
        candidate = state.candidates.get(candidate_id)
        if candidate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "candidate_not_found", "candidate_id": candidate_id},
            )
        if candidate.review_status != "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "not_approved",
                    "current_status": candidate.review_status,
                },
            )
        if candidate.imported_doc_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "already_imported",
                    "imported_doc_id": candidate.imported_doc_id,
                },
            )
        try:
            doc_id = import_candidate_to_knowledge_api(candidate)
        except KnowledgeApiUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "knowledge_api_unavailable",
                    "message": str(exc),
                },
            ) from exc
        except KnowledgeApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "knowledge_api_error",
                    "status_code": exc.status_code,
                    "message": exc.body,
                },
            ) from exc
        updated = candidate.model_copy(update={"imported_doc_id": doc_id})
        state.candidates[candidate_id] = updated
        if hasattr(state, "save_candidate"):
            state.save_candidate(updated)
        return updated

    # ------------------------------------------------------------------ #
    # Ticket write-back (spec §6.4)
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v1/tickets/{ticket_id}/rca-summary",
        response_model=TicketWritebackResponse,
    )
    def writeback_rca_summary(
        ticket_id: str,
        payload: TicketWritebackRequest,
        _principal: OIDCOrInternalPrincipal = Depends(write_auth),
    ) -> TicketWritebackResponse:
        report = state.reports.get(payload.rca_report_id)
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "rca_report_not_found",
                    "rca_report_id": payload.rca_report_id,
                },
            )
        adapter = state.writeback_adapter
        audit = state.writebacks
        adapter_name = getattr(adapter, "name", type(adapter).__name__)
        try:
            response = adapter.post_summary(  # type: ignore[union-attr]
                ticket_id=ticket_id,
                rca_report_id=payload.rca_report_id,
                incident_id=report.incident_id,
                summary_markdown=report.report_markdown,
                final_root_cause=report.final_root_cause,
            )
        except TicketWritebackUnavailable as exc:
            record = audit.record(  # type: ignore[union-attr]
                ticket_id=ticket_id,
                rca_report_id=payload.rca_report_id,
                incident_id=report.incident_id,
                status="failed",
                adapter_name=adapter_name,
                response={},
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "ticket_writeback_unavailable",
                    "message": str(exc),
                    "attempt_id": record.attempt_id,
                },
            ) from exc
        except TicketWritebackError as exc:
            record = audit.record(  # type: ignore[union-attr]
                ticket_id=ticket_id,
                rca_report_id=payload.rca_report_id,
                incident_id=report.incident_id,
                status="failed",
                adapter_name=adapter_name,
                response={},
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "ticket_writeback_rejected",
                    "status_code": exc.status_code,
                    "message": exc.body,
                    "attempt_id": record.attempt_id,
                },
            ) from exc
        record = audit.record(  # type: ignore[union-attr]
            ticket_id=ticket_id,
            rca_report_id=payload.rca_report_id,
            incident_id=report.incident_id,
            status="success",
            adapter_name=adapter_name,
            response=response,
            error=None,
        )
        return TicketWritebackResponse(
            ticket_id=ticket_id,
            rca_report_id=payload.rca_report_id,
            incident_id=report.incident_id,
            adapter_name=adapter_name,
            status="success",
            response=response,
            attempt_id=record.attempt_id,
        )

    @app.get("/api/v1/tickets/{ticket_id}/rca-summary/attempts")
    def list_writeback_attempts(ticket_id: str) -> dict[str, object]:
        audit = state.writebacks
        attempts = audit.list_for_ticket(ticket_id)  # type: ignore[union-attr]
        return {
            "ticket_id": ticket_id,
            "items": [
                {
                    "attempt_id": a.attempt_id,
                    "rca_report_id": a.rca_report_id,
                    "status": a.status,
                    "adapter_name": a.adapter_name,
                    "error": a.error,
                    "created_at": a.created_at,
                }
                for a in attempts
            ],
            "total": len(attempts),
        }

    @app.get("/api/v1/metrics/operations")
    def operational_metrics() -> dict[str, object]:
        metrics = compute_metrics(state)
        return {
            "tool_call_success_rate": metrics.tool_call_success_rate,
            "human_acceptance_rate": metrics.human_acceptance_rate,
            "alert_compression_ratio": metrics.alert_compression_ratio,
            "report_gen_seconds_avg": metrics.report_gen_seconds_avg,
            "raw": metrics.raw,
        }

    return app


def _default_store() -> RcaStore:
    # R28-PG: honour DATABASE_URL via build_rca_store() (PgRcaStore when
    # set).  Pre-R28 this only checked RCA_SQLITE_PATH and silently
    # ignored PG, leaving all RCA data in-memory or SQLite.
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        from ai_employee.rca_agent.pg_store import build_rca_store

        store = build_rca_store(database_url=database_url)
    else:
        sqlite_path = os.getenv("RCA_SQLITE_PATH")
        if sqlite_path:
            store = SQLiteRcaStore(sqlite_path)
        else:
            store = RcaStore()
    # R27: attach a Neo4j topology client when NEO4J_URL is set.  The
    # ``build_topology_client`` factory is fail-closed (returns None on
    # any connectivity failure), so this attachment is always safe.
    from ai_employee.rca_agent.topology import build_topology_client

    store.topology_client = build_topology_client()  # type: ignore[attr-defined]
    return store


def _page_bounds(page: int, page_size: int) -> tuple[int, int, int, int]:
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))
    start = (page - 1) * page_size
    end = start + page_size
    return page, page_size, start, end


def _generate_and_persist_candidates(store: RcaStore, report: RcaReportResponse) -> None:
    incident = store.incidents.get(report.incident_id)
    if incident is None:
        return
    candidates = generate_candidates_from_report(report, incident, report.evidence)
    if not candidates:
        return
    now = datetime.now(timezone.utc).isoformat()
    for candidate in candidates:
        store.candidate_count += 1
        candidate_id = f"ck_{store.candidate_count:03d}"
        persisted = candidate.model_copy(update={"candidate_id": candidate_id, "created_at": now})
        store.candidates[candidate_id] = persisted
        if hasattr(store, "save_candidate"):
            store.save_candidate(persisted)


app = create_app()
