from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, status

from ai_employee.rca_agent.runtime import (
    RcaStore,
    build_incident,
    normalize_alarm,
    resume_with_more_evidence,
    run_rca,
)
from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    IncidentBuildRequest,
    IncidentResponse,
    RawAlarmEvent,
    ReportReviewRequest,
    ReportReviewResponse,
    RcaReportResponse,
    RcaReportListResponse,
    RcaReportSummary,
    RcaRunCreate,
    RcaRunListResponse,
    RcaRunResponse,
    RcaRunSummary,
)
from ai_employee.rca_agent.store import SQLiteRcaStore

SERVICE_VERSION = "0.1.0"


def create_app(store: RcaStore | None = None) -> FastAPI:
    app = FastAPI(title="AI Employee RCA Agent", version=SERVICE_VERSION)
    state = store or _default_store()

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
    def create_alarm_event(payload: RawAlarmEvent) -> AlarmEvent:
        return normalize_alarm(state, payload)

    @app.post(
        "/api/v1/incidents/build",
        response_model=IncidentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_incident(payload: IncidentBuildRequest) -> IncidentResponse:
        return build_incident(state, payload.alarms, payload.time_window_minutes)

    @app.post(
        "/api/v1/rca/runs",
        response_model=RcaRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_rca_run(payload: RcaRunCreate) -> RcaRunResponse:
        if not payload.incident_id and not payload.alarms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "incident_or_alarms_required"},
            )
        if payload.incident_id and payload.incident_id not in state.incidents and not payload.alarms:
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
            reports = [
                report for report in reports if report.review_status == review_status
            ]
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
    def review_report(report_id: str, payload: ReportReviewRequest) -> ReportReviewResponse:
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
        return ReportReviewResponse(
            report_id=report_id,
            review_status=payload.decision,
            final_root_cause=payload.final_root_cause,
            reviewer=payload.reviewer,
            comment=payload.comment,
        )

    return app


def _default_store() -> RcaStore:
    sqlite_path = os.getenv("RCA_SQLITE_PATH")
    if sqlite_path:
        return SQLiteRcaStore(sqlite_path)
    return RcaStore()


def _page_bounds(page: int, page_size: int) -> tuple[int, int, int, int]:
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))
    start = (page - 1) * page_size
    end = start + page_size
    return page, page_size, start, end


app = create_app()
