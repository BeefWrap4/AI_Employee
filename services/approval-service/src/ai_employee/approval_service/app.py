"""approval-service FastAPI app.

Exposes the approval-task lifecycle over HTTP so the agent-platform can
delegate approval calls (spec §9 ``approval-service``).  Contracts
mirror the platform's R20 governance endpoints so consumers can switch
to the service with no contract drift.

Endpoints:

* ``POST /api/v1/approval-tasks``            — create a task
* ``GET  /api/v1/approval-tasks``            — list (status filter + paging)
* ``GET  /api/v1/approval-tasks/{id}``       — fetch one
* ``POST /api/v1/approval-tasks/{id}/decision``
* ``POST /api/v1/approvals/{id}/supplement``
* ``POST /api/v1/approvals/{id}/supplement/resolve``
* ``POST /api/v1/approvals/{id}/transfer``
* ``POST /api/v1/approvals/{id}/escalate``
"""

from __future__ import annotations

from ai_employee.approval_service import state_machine as sm
from ai_employee.approval_service.schemas import (
    ApprovalDecisionRequest,
    ApprovalEscalateRequest,
    ApprovalSupplementGovernanceRequest,
    ApprovalSupplementResolveRequest,
    ApprovalTask,
    ApprovalTaskCreate,
    ApprovalTaskListResponse,
    ApprovalTransferRequest,
)
from ai_employee.approval_service.store import ApprovalTaskStore
from fastapi import FastAPI, HTTPException, status

SERVICE_VERSION = "0.1.0"


def create_app(store: ApprovalTaskStore | None = None) -> FastAPI:
    app = FastAPI(title="AI Employee Approval Service", version=SERVICE_VERSION)
    # R25-L: shared rate-limit middleware (no-op unless RATE_LIMIT_ENABLED=true).
    from ai_employee.rate_limit import install_rate_limiter

    install_rate_limiter(app)
    state = store or ApprovalTaskStore()

    def _get_or_404(task_id: str) -> dict:
        task = state.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        return task

    def _persist(task: dict) -> ApprovalTask:
        state.upsert(task)
        return ApprovalTask(**task)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"service": "approval-service", "status": "ok", "version": SERVICE_VERSION}

    @app.post(
        "/api/v1/approval-tasks",
        response_model=ApprovalTask,
        status_code=status.HTTP_201_CREATED,
    )
    def create_approval_task(payload: ApprovalTaskCreate) -> ApprovalTask:
        existing = state.get(payload.task_id)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_already_exists",
                    "task_id": payload.task_id,
                },
            )
        task = sm.create_task(payload.model_dump())
        return _persist(task)

    @app.get("/api/v1/approval-tasks/{task_id}", response_model=ApprovalTask)
    def get_approval_task(task_id: str) -> ApprovalTask:
        return ApprovalTask(**_get_or_404(task_id))

    @app.get("/api/v1/approval-tasks", response_model=ApprovalTaskListResponse)
    def list_approval_tasks(
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ApprovalTaskListResponse:
        items, total = state.list(status=status, page=page, page_size=page_size)
        page_n = max(1, int(page))
        page_size_n = max(1, min(200, int(page_size)))
        return ApprovalTaskListResponse(
            items=[ApprovalTask(**item) for item in items],
            total=total,
            page=page_n,
            page_size=page_size_n,
        )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/decision",
        response_model=ApprovalTask,
    )
    def decide_approval(task_id: str, payload: ApprovalDecisionRequest) -> ApprovalTask:
        task = _get_or_404(task_id)
        if not sm.is_decidable(task):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_already_decided",
                    "task_id": task_id,
                    "current_status": task["status"],
                },
            )
        updated = sm.decide(
            task,
            decision=payload.decision,
            decided_by=payload.decided_by,
            comment=payload.comment,
        )
        return _persist(updated)

    @app.post(
        "/api/v1/approvals/{task_id}/supplement",
        response_model=ApprovalTask,
    )
    def supplement_governance(
        task_id: str,
        payload: ApprovalSupplementGovernanceRequest,
    ) -> ApprovalTask:
        task = _get_or_404(task_id)
        try:
            updated = sm.request_supplement_governance(
                task,
                note=payload.note,
                attachments=[a.model_dump() for a in payload.attachments],
                requested_by=payload.requested_by,
            )
        except sm.ApprovalTaskNotSupplementable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_supplementable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        return _persist(updated)

    @app.post(
        "/api/v1/approvals/{task_id}/supplement/resolve",
        response_model=ApprovalTask,
    )
    def supplement_resolve(
        task_id: str,
        payload: ApprovalSupplementResolveRequest,
    ) -> ApprovalTask:
        task = _get_or_404(task_id)
        try:
            updated = sm.resolve_supplement_governance(
                task,
                attachments=[a.model_dump() for a in payload.attachments],
                note=payload.note,
                resolved_by=payload.resolved_by,
            )
        except sm.ApprovalSupplementStateConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "not_supplement_pending",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        return _persist(updated)

    @app.post(
        "/api/v1/approvals/{task_id}/transfer",
        response_model=ApprovalTask,
    )
    def transfer_governance(
        task_id: str,
        payload: ApprovalTransferRequest,
    ) -> ApprovalTask:
        task = _get_or_404(task_id)
        try:
            updated = sm.transfer_approval(
                task,
                new_approver=payload.new_approver,
                reason=payload.reason,
                transferred_by=payload.transferred_by,
                is_admin=payload.is_admin,
            )
        except sm.ApprovalTransferForbidden as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "approval_transfer_forbidden",
                    "task_id": task_id,
                    "actor": str(exc),
                },
            )
        except sm.ApprovalTaskNotSupplementable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_transferable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        return _persist(updated)

    @app.post(
        "/api/v1/approvals/{task_id}/escalate",
        response_model=ApprovalTask,
    )
    def escalate_governance(
        task_id: str,
        payload: ApprovalEscalateRequest,
    ) -> ApprovalTask:
        task = _get_or_404(task_id)
        try:
            updated = sm.escalate_approval(
                task,
                escalated_to=payload.escalated_to,
                reason=payload.reason,
                escalated_by=payload.escalated_by,
            )
        except sm.ApprovalTaskNotSupplementable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_escalatable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        return _persist(updated)

    return app


app = create_app()
