from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from ai_employee.agent_platform_api.clients import (
    ApprovalServiceClient,
    InMemoryApprovalServiceClient,
    InMemoryMcpGatewayClient,
    McpGatewayClient,
    _ApprovalError,
    _McpError,
    apply_decision_run_effect,
    build_approval_client,
    build_mcp_client,
)
from ai_employee.agent_platform_api.eval_compare import compare_reports
from ai_employee.agent_platform_api.eval_store import EvalStore
from ai_employee.agent_platform_api.events import bus as platform_bus
from ai_employee.agent_platform_api.inspection import (
    run_inspection,
    write_inspection_log,
)
from ai_employee.agent_platform_api.platform_metrics import (
    metrics as platform_metrics,
)
from ai_employee.agent_platform_api.platform_metrics import (
    snapshot_dict,
    snapshot_timeseries,
)
from ai_employee.agent_platform_api.run_store import AgentRunStore
from ai_employee.agent_platform_api.runtime import (
    TEMPLATES,
    AgentPlatformStore,
    ApprovalSupplementStateConflict,
    ApprovalTaskNotFound,
    ApprovalTaskNotModifiable,
    ApprovalTaskNotSupplementable,
    ApprovalTransferForbidden,
    answer_supplement,
    create_run,
    expire_approval,
    list_templates,
    request_supplement,
    resume_run_from_node,
    route_approval,
    run_to_persist_dict,
    select_runtime,
)
from ai_employee.agent_platform_api.schemas import (
    AgentRunCreate,
    AgentRunListResponse,
    AgentRunResponse,
    AgentRunResumeResponse,
    AgentRunSummary,
    AgentRunTraceResponse,
    AgentTemplateListResponse,
    ApprovalDecisionRequest,
    ApprovalDelegateRequest,
    ApprovalEscalateRequest,
    ApprovalRouteRequest,
    ApprovalSupplementAnswer,
    ApprovalSupplementGovernanceRequest,
    ApprovalSupplementRequest,
    ApprovalSupplementResolveRequest,
    ApprovalTask,
    ApprovalTaskListResponse,
    ApprovalTimeoutRequest,
    ApprovalTransferRequest,
    EvalRunListItem,
    EvalRunListResponse,
    EvalRunRequest,
    EvalRunResponse,
    ToolListResponse,
    ToolRegistration,
    ToolResponse,
)
from ai_employee.auth_policy.fastapi_dep import (
    OIDCOrInternalPrincipal,
    require_oidc_or_internal,
)
from ai_employee.common_schemas.eval import (
    UnifiedReport,
    to_unified_rag,
    to_unified_rca,
)
from ai_employee.common_schemas.idempotency import (
    IdempotencyStore,
    build_idempotency_store,
)
from ai_employee.observability import render_prometheus_text
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response

SERVICE_VERSION = "0.1.0"
EVAL_TOP_KS = [1, 3, 5]


def _map_approval_error(exc: _ApprovalError) -> HTTPException:
    """Re-raise an approval-service upstream error as a FastAPI HTTPException.

    The service returns the same ``detail`` shape the platform surfaces
    (``{"error_code": ..., ...}``), so we forward it verbatim with the
    upstream status code.
    """
    detail = exc.body.get("detail", exc.body)
    return HTTPException(status_code=exc.status_code, detail=detail)


def _map_mcp_error(exc: _McpError) -> HTTPException:
    """Re-raise an mcp-gateway upstream error as a FastAPI HTTPException."""
    detail = exc.body.get("detail", exc.body)
    return HTTPException(status_code=exc.status_code, detail=detail)


def _sync_task_locally(
    client: ApprovalServiceClient,
    store: AgentPlatformStore,
    task: ApprovalTask,
) -> None:
    """Mirror a service-returned task into the platform's in-memory store.

    In HTTP mode the service is the source of truth for task state; the
    platform keeps a local copy so the trace endpoint and legacy HITL
    endpoints stay consistent.  In-memory mode is a no-op (the client
    already mutated the store).
    """
    if client.applies_run_side_effects:
        return
    store.approval_tasks[task.task_id] = task


def _resolve_idempotency_store(
    override: IdempotencyStore | None,
) -> IdempotencyStore:
    """Return the idempotency store to use for this app instance.

    ``override`` lets tests inject an :class:`InMemoryIdempotencyStore`.
    Otherwise the store is built from env (``REDIS_URL`` → Redis, with
    graceful fallback to in-memory).  Even when no header is sent the
    store is harmless: ``get_or_begin`` is only called for keys that
    are present, so the default in-memory store stays empty.
    """
    if override is not None:
        return override
    return build_idempotency_store()


def _idempotency_key(request: Request) -> str | None:
    """Return a non-empty ``Idempotency-Key`` header, or None."""
    raw = request.headers.get("Idempotency-Key")
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def create_app(
    store: AgentPlatformStore | None = None,
    eval_store: EvalStore | None = None,
    run_store: AgentRunStore | None = None,
    *,
    approval_client: ApprovalServiceClient | None = None,
    mcp_client: McpGatewayClient | None = None,
    idempotency_store: IdempotencyStore | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Agent Platform API", version=SERVICE_VERSION)
    state = store or AgentPlatformStore()
    eval_state = eval_store or EvalStore()
    # R28-PG: honour DATABASE_URL via build_run_store() (PgAgentRunStore
    # when set).  Pre-R28 this hardcoded AgentRunStore() (SQLite) and
    # silently ignored PG.
    if run_store is None:
        from ai_employee.agent_platform_api.pg_run_store import build_run_store

        run_state = build_run_store()
    else:
        run_state = run_store
    idem_store = _resolve_idempotency_store(idempotency_store)

    # R24-A.4: production write endpoints (agent-run creation, approval
    # decisions, eval runs) require authentication via OIDC (RS256)
    # when SSO is enabled, the legacy HS256 JWT, or the
    # ``X-Internal-Token`` shared secret.  Each tier uses the matching
    # RBAC permission so OIDC/JWT principals are checked while legacy
    # internal-service callers remain trusted.
    run_auth = require_oidc_or_internal(permissions=["agent:run"])
    approval_auth = require_oidc_or_internal(permissions=["agent:approve"])
    eval_auth = require_oidc_or_internal(permissions=["knowledge:read"])

    # R23-3: when EVENT_BUS_BACKEND=redis (and REDIS_URL is reachable),
    # wrap the in-process bus in a RedisEventBus so run events published
    # on one replica are delivered to WebSocket subscribers on another.
    # The Redis bus shares the local ``platform_bus`` singleton, so the
    # WebSocket endpoint below keeps working unchanged.
    from ai_employee.agent_platform_api.events import (
        RedisEventBus,
        build_multi_replica_event_bus,
    )

    multi_replica_bus = build_multi_replica_event_bus()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):  # pragma: no cover - lifecycle
        if isinstance(multi_replica_bus, RedisEventBus):
            multi_replica_bus.start_listener()
        yield
        if isinstance(multi_replica_bus, RedisEventBus):
            multi_replica_bus.stop_listener()

    app.router.lifespan_context = _lifespan

    # R21 service isolation (spec §9): delegate approval-task state to a
    # standalone ``approval-service`` when ``APPROVAL_SERVICE_URL`` is
    # set, and tool registry / discovery / invocation to the
    # ``mcp-gateway`` when ``MCP_GATEWAY_URL`` is set.  In-memory clients
    # are bound to this app's store so they share state with the legacy
    # endpoints.
    approval_state = approval_client or build_approval_client()
    if isinstance(approval_state, InMemoryApprovalServiceClient):
        approval_state.bind(state)
    mcp_state = mcp_client or build_mcp_client(state)
    if isinstance(mcp_state, InMemoryMcpGatewayClient):
        mcp_state.bind(state)

    from ai_employee.agent_platform_api.tenant import (
        reset_current_tenant,
        resolve_tenant_context,
        set_current_tenant_id,
    )

    @app.middleware("http")
    async def tenant_middleware(request, call_next):
        from starlette.responses import JSONResponse

        explicit = request.query_params.get("tenant_id")
        header_tenant = request.headers.get("X-Tenant-ID")
        claims_sub = request.headers.get("X-User-Sub")  # populated by auth layer
        try:
            ctx = resolve_tenant_context(
                explicit=explicit,
                header_tenant=header_tenant,
                claims_sub=claims_sub,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error_code": "invalid_tenant_id", "message": str(exc)},
            )
        token = set_current_tenant_id(ctx.tenant_id)
        try:
            response = await call_next(request)
        finally:
            reset_current_tenant(token)
        # Echo resolved tenant for client visibility.
        response.headers["X-Tenant-ID"] = ctx.tenant_id
        return response

    @app.get("/api/v1/tenant/whoami")
    def tenant_whoami(request: Request) -> dict[str, object]:
        """Return the resolved tenant context for the current request."""
        from ai_employee.agent_platform_api.tenant import (
            get_current_tenant_id,
            resolve_tenant_context,
        )

        explicit = request.query_params.get("tenant_id")
        header_tenant = request.headers.get("X-Tenant-ID")
        claims_sub = request.headers.get("X-User-Sub")
        ctx = resolve_tenant_context(
            explicit=explicit,
            header_tenant=header_tenant,
            claims_sub=claims_sub,
        )
        return {
            "tenant_id": get_current_tenant_id(),
            "user_id": ctx.user_id,
            "source": ctx.source,
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "agent-platform-api",
            "status": "ok",
            "version": SERVICE_VERSION,
            "runtime": "in_memory",
        }

    from ai_employee.agent_platform_api.audit_api import mount_audit_endpoints

    mount_audit_endpoints(app)

    from ai_employee.agent_platform_api.rate_limit_middleware import (
        install_rate_limiter,
    )

    install_rate_limiter(app)

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        """Readiness probe — checks configured downstream deps.

        Returns 200 when all deps are healthy, 503 otherwise so k8s
        stops routing traffic to this pod (without restarting it).
        Deps are configured via env: ``SQLITE_PATH`` (sqlite),
        ``REDIS_URL`` (redis).  Missing env = dep not configured.
        """
        from ai_employee.agent_platform_api.health import (
            ReadinessResult,
            check_redis,
            check_sqlite,
        )

        checks = []
        sqlite_path = os.environ.get("SQLITE_PATH")
        if sqlite_path:
            checks.append(check_sqlite(sqlite_path))
        redis_url = os.environ.get("REDIS_URL")
        if redis_url:
            checks.append(check_redis(redis_url))
        result = ReadinessResult(checks=checks)
        status_code = 200 if result.ready else 503
        return JSONResponse(status_code=status_code, content=result.to_dict())

    @app.get("/api/v1/agent-templates", response_model=AgentTemplateListResponse)
    def get_agent_templates() -> AgentTemplateListResponse:
        items = list_templates()
        return AgentTemplateListResponse(items=items, total=len(items))

    @app.post(
        "/api/v1/agent-runs",
        response_model=AgentRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent_run(
        payload: AgentRunCreate,
        request: Request,
        _principal: OIDCOrInternalPrincipal = Depends(run_auth),
    ) -> AgentRunResponse:
        # R23: honour an Idempotency-Key header so a retried POST
        # (client timeout + replay, or a load-balancer redirect to
        # another replica) returns the original run verbatim instead
        # of creating a duplicate.
        idem_key = _idempotency_key(request)
        if idem_key is not None:
            rec = idem_store.get_or_begin(idem_key)
            if rec.status in {"success", "failed"} and rec.result is not None:
                return AgentRunResponse(**rec.result["body"])
        if payload.template_id not in TEMPLATES:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "agent_template_not_found",
                    "template_id": payload.template_id,
                },
            )
        run = create_run(state, payload, runtime=select_runtime())
        # R21: when delegating to a standalone approval-service, push the
        # newly-created approval task to the service so it owns the state
        # machine.  In-memory mode is a no-op (the task already lives in
        # ``state.approval_tasks``).
        if run.approval_status == "pending":
            task = state.approval_tasks.get(
                next(
                    (tid for tid, t in state.approval_tasks.items() if t.run_id == run.run_id),
                    "",
                )
            )
            if task is not None:
                try:
                    approval_state.create_task(task)
                except _ApprovalError:
                    # Service already has the task (e.g. replay) — ignore.
                    pass
        run_state.upsert_run(run_to_persist_dict(run))
        platform_metrics().record_run(succeeded=(run.status != "failed"))
        if idem_key is not None:
            idem_store.complete(
                idem_key,
                status="success" if run.status != "failed" else "failed",
                result={"body": run.model_dump(mode="json")},
            )
        return run

    @app.post(
        "/api/v1/agent-runs/{run_id}/resume",
        response_model=AgentRunResumeResponse,
    )
    def resume_agent_run(run_id: str) -> AgentRunResumeResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "agent_run_not_found", "run_id": run_id},
            )
        if run.status == "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "agent_run_already_completed",
                    "run_id": run_id,
                },
            )
        previous_node = run.node_trace[-1].node_name if run.node_trace else "TemplateLoaded"
        updated = resume_run_from_node(state, run_id)
        persisted = run_to_persist_dict(updated)
        persisted["new_events"] = [
            {
                "node_name": "ResumeNode",
                "status": "completed",
                "detail": f"Resumed after {previous_node}",
            }
        ]
        run_state.upsert_run(persisted)
        run_state.mark_resumed(run_id, resume_from_node=previous_node)
        return AgentRunResumeResponse(
            run=updated,
            resumed_from_node=previous_node,
        )

    @app.get("/api/v1/agent-runs", response_model=AgentRunListResponse)
    def list_agent_runs(
        template_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> AgentRunListResponse:
        runs = list(state.runs.values())
        if template_id is not None:
            runs = [run for run in runs if run.template_id == template_id]
        if status is not None:
            runs = [run for run in runs if run.status == status]
        total = len(runs)
        page, page_size, start, end = _page_bounds(page, page_size)
        return AgentRunListResponse(
            items=[
                AgentRunSummary(
                    run_id=run.run_id,
                    template_id=run.template_id,
                    agent_name=run.agent_name,
                    status=run.status,
                    trace_id=run.trace_id,
                    requested_by=run.requested_by,
                    approval_status=run.approval_status,
                )
                for run in runs[start:end]
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get(
        "/api/v1/agent-runs/{run_id}/trace",
        response_model=AgentRunTraceResponse,
    )
    def get_agent_run_trace(run_id: str) -> AgentRunTraceResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "agent_run_not_found", "run_id": run_id},
            )
        template = TEMPLATES[run.template_id]
        approval_tasks = [task for task in state.approval_tasks.values() if task.run_id == run_id]
        registered_tools = [
            tool for tool in state.tools.values() if tool.tool_name in template.tool_names
        ]
        return AgentRunTraceResponse(
            run=run,
            template=template,
            node_trace=run.node_trace,
            tool_calls=run.tool_calls,
            approval_tasks=approval_tasks,
            registered_tools=registered_tools,
        )

    @app.get("/api/v1/agent-runs/{run_id}", response_model=AgentRunResponse)
    def get_agent_run(run_id: str) -> AgentRunResponse:
        run = state.runs.get(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "agent_run_not_found", "run_id": run_id},
            )
        return run

    @app.get("/api/v1/approval-tasks", response_model=ApprovalTaskListResponse)
    def list_approval_tasks(
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ApprovalTaskListResponse:
        page, page_size, _, _ = _page_bounds(page, page_size)
        data = approval_state.list_tasks(status=status, page=page, page_size=page_size)
        return ApprovalTaskListResponse(
            items=[ApprovalTask(**item) for item in data["items"]],
            total=data["total"],
            page=data["page"],
            page_size=data["page_size"],
        )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/decision",
        response_model=ApprovalTask,
    )
    def decide_approval(
        task_id: str,
        payload: ApprovalDecisionRequest,
        _principal: OIDCOrInternalPrincipal = Depends(approval_auth),
    ) -> ApprovalTask:
        from ai_employee.agent_platform_api.runtime import (
            ApprovalTaskNotFound,
            ApprovalTaskNotModifiable,
        )

        try:
            updated_task = approval_state.decide(
                task_id=task_id,
                decision=payload.decision,
                decided_by=payload.decided_by,
                comment=payload.comment,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTaskNotModifiable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_already_decided",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        except _ApprovalError as exc:
            raise _map_approval_error(exc)

        # When the client did NOT already mutate the platform's run
        # store (HTTP mode), apply the run side-effect locally.  The
        # in-memory client does this inside ``decide``.
        if not approval_state.applies_run_side_effects:
            apply_decision_run_effect(
                state,
                task=updated_task,
                decision=payload.decision,
                comment=payload.comment,
                decided_by=payload.decided_by,
            )
            _sync_task_locally(approval_state, state, updated_task)

        # Approval wait time: task.created_at → now.  Best-effort; missing
        # timestamps are skipped silently.
        try:
            from datetime import datetime as _dt

            _task = updated_task
            if getattr(_task, "created_at", None):
                _created = _task.created_at
                if _created.endswith("Z"):
                    _created = _created.replace("Z", "+00:00")
                _wait = (_dt.now(_dt.timezone.utc) - _dt.fromisoformat(_created)).total_seconds()
                if _wait >= 0:
                    platform_metrics().record_approval(_wait)
        except Exception:
            pass
        platform_metrics().record_review(accepted=(payload.decision == "approved"))
        run = state.runs.get(updated_task.run_id)
        if run is not None:
            run_state.upsert_run(run_to_persist_dict(run))
        return updated_task

    @app.post(
        "/api/v1/approval-tasks/{task_id}/supplement-request",
        response_model=ApprovalTask,
    )
    def supplement_request(
        task_id: str,
        payload: ApprovalSupplementRequest,
    ) -> ApprovalTask:
        try:
            return request_supplement(
                state,
                task_id=task_id,
                question=payload.question,
                requested_by=payload.requested_by,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTaskNotModifiable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_supplementable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/supplement-answer",
        response_model=ApprovalTask,
    )
    def supplement_answer(
        task_id: str,
        payload: ApprovalSupplementAnswer,
    ) -> ApprovalTask:
        try:
            return answer_supplement(
                state,
                task_id=task_id,
                answer=payload.answer,
                answered_by=payload.answered_by,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error_code": "not_pending_supplement", "message": str(exc)},
            )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/route",
        response_model=ApprovalTask,
    )
    def route_task(
        task_id: str,
        payload: ApprovalRouteRequest,
    ) -> ApprovalTask:
        try:
            return route_approval(
                state,
                task_id=task_id,
                routed_to=payload.routed_to,
                routed_by=payload.routed_by,
                reason=payload.reason,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTaskNotModifiable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_modifiable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/timeout",
        response_model=ApprovalTask,
    )
    def timeout_task(
        task_id: str,
        payload: ApprovalTimeoutRequest,
    ) -> ApprovalTask:
        try:
            return expire_approval(
                state,
                task_id=task_id,
                escalation_reviewer=payload.escalation_reviewer,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTaskNotModifiable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_modifiable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/delegate",
        response_model=ApprovalTask,
    )
    def delegate_task(
        task_id: str,
        payload: ApprovalDelegateRequest,
    ) -> ApprovalTask:
        from ai_employee.agent_platform_api.runtime import delegate_approval

        task = state.approval_tasks.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        try:
            return delegate_approval(
                state,
                task_id=task_id,
                delegate=payload.delegate,
                delegated_by=payload.delegated_by,
                reason=payload.reason,
            )
        except ApprovalTaskNotModifiable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_modifiable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )

    # ------------------------------------------------------------------ #
    # R20 governance endpoints (spec §5.4): supplement / transfer / escalate
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v1/approvals/{task_id}/supplement",
        response_model=ApprovalTask,
    )
    def supplement_governance(
        task_id: str,
        payload: ApprovalSupplementGovernanceRequest,
    ) -> ApprovalTask:
        from ai_employee.agent_platform_api.object_refs import normalize_attachments

        try:
            normalized = normalize_attachments(payload.attachments)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "invalid_attachment", "message": str(exc)},
            ) from exc
        try:
            updated = approval_state.request_supplement(
                task_id=task_id,
                note=payload.note,
                attachments=normalized,
                requested_by=payload.requested_by,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTaskNotSupplementable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_supplementable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        except _ApprovalError as exc:
            raise _map_approval_error(exc)
        _sync_task_locally(approval_state, state, updated)
        return updated

    @app.post(
        "/api/v1/approvals/{task_id}/supplement/resolve",
        response_model=ApprovalTask,
    )
    def supplement_resolve(
        task_id: str,
        payload: ApprovalSupplementResolveRequest,
    ) -> ApprovalTask:
        from ai_employee.agent_platform_api.object_refs import normalize_attachments

        try:
            normalized = normalize_attachments(payload.attachments)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "invalid_attachment", "message": str(exc)},
            ) from exc
        try:
            updated = approval_state.resolve_supplement(
                task_id=task_id,
                attachments=normalized,
                note=payload.note,
                resolved_by=payload.resolved_by,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalSupplementStateConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "not_supplement_pending",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        except _ApprovalError as exc:
            raise _map_approval_error(exc)
        _sync_task_locally(approval_state, state, updated)
        return updated

    @app.post(
        "/api/v1/approvals/{task_id}/transfer",
        response_model=ApprovalTask,
    )
    def transfer_governance(
        task_id: str,
        payload: ApprovalTransferRequest,
    ) -> ApprovalTask:
        try:
            updated = approval_state.transfer(
                task_id=task_id,
                new_approver=payload.new_approver,
                reason=payload.reason,
                transferred_by=payload.transferred_by,
                is_admin=payload.is_admin,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTransferForbidden as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "approval_transfer_forbidden",
                    "task_id": task_id,
                    "actor": str(exc),
                },
            )
        except ApprovalTaskNotSupplementable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_transferable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        except _ApprovalError as exc:
            raise _map_approval_error(exc)
        _sync_task_locally(approval_state, state, updated)
        return updated

    @app.post(
        "/api/v1/approvals/{task_id}/escalate",
        response_model=ApprovalTask,
    )
    def escalate_governance(
        task_id: str,
        payload: ApprovalEscalateRequest,
    ) -> ApprovalTask:
        try:
            updated = approval_state.escalate(
                task_id=task_id,
                escalated_to=payload.escalated_to,
                reason=payload.reason,
                escalated_by=payload.escalated_by,
            )
        except ApprovalTaskNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        except ApprovalTaskNotSupplementable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_not_escalatable",
                    "task_id": task_id,
                    "current_status": str(exc),
                },
            )
        except _ApprovalError as exc:
            raise _map_approval_error(exc)
        _sync_task_locally(approval_state, state, updated)
        return updated

    @app.post(
        "/api/v1/tools",
        response_model=ToolResponse,
        response_model_exclude_none=True,
        status_code=status.HTTP_201_CREATED,
    )
    def create_tool(payload: ToolRegistration) -> ToolResponse:
        if payload.tool_name in state.tools and isinstance(mcp_state, InMemoryMcpGatewayClient):
            # Legacy duplicate check (in-memory only — the HTTP client
            # lets the gateway enforce uniqueness and re-raises the
            # upstream 409 below).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "tool_already_registered",
                    "tool_name": payload.tool_name,
                },
            )
        try:
            return mcp_state.register(payload.model_dump())
        except _McpError as exc:
            raise _map_mcp_error(exc)

    @app.get("/api/v1/tools", response_model=ToolListResponse)
    def list_tools(
        risk_level: str | None = None,
        status: str | None = None,
        service_name: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ToolListResponse:
        data = mcp_state.list_tools(
            risk_level=risk_level,
            status=status,
            service_name=service_name,
            page=page,
            page_size=page_size,
        )
        return ToolListResponse(
            items=[ToolResponse(**item) for item in data["items"]],
            total=data["total"],
            page=data["page"],
            page_size=data["page_size"],
        )

    # ------------------------------------------------------------------ #
    # Eval center (spec §7)
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v1/evaluations/runs",
        response_model=EvalRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_eval_run(
        payload: EvalRunRequest,
        request: Request,
        _principal: OIDCOrInternalPrincipal = Depends(eval_auth),
    ) -> EvalRunResponse:
        # R23: idempotency — a replayed eval POST returns the cached
        # eval_run_id instead of re-running the (expensive) eval.
        idem_key = _idempotency_key(request)
        if idem_key is not None:
            rec = idem_store.get_or_begin(idem_key)
            if rec.status in {"success", "failed"} and rec.result is not None:
                return EvalRunResponse(**rec.result["body"])
        eval_run_id = eval_state.create_eval_run(
            eval_type=payload.eval_type,
            template_id=payload.template_id,
            golden_path=payload.golden_path,
        )
        try:
            if payload.eval_type == "rag":
                unified = _execute_rag_eval(payload.golden_path, payload.api_base)
            else:
                unified = _execute_rca_eval(payload.golden_path)
        except ValueError as exc:
            eval_state.fail_eval_run(eval_run_id, error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "eval_invalid_request",
                    "eval_run_id": eval_run_id,
                    "error": str(exc),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            eval_state.fail_eval_run(eval_run_id, error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "eval_failed",
                    "eval_run_id": eval_run_id,
                    "error": str(exc),
                },
            )

        summary = {
            "total": unified.total,
            "top1_coverage": unified.top1_coverage,
            "top3_coverage": unified.top3_coverage,
            "evidence_coverage": unified.evidence_coverage,
        }
        eval_state.complete_eval_run(eval_run_id, report=unified.to_dict(), summary=summary)
        record = eval_state.get_eval_run(eval_run_id)
        response = _record_to_response(record)
        if idem_key is not None:
            idem_store.complete(
                idem_key,
                status="success",
                result={"body": response.model_dump(mode="json")},
            )
        return response

    @app.get("/api/v1/evaluations/runs", response_model=EvalRunListResponse)
    def list_eval_runs(
        eval_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> EvalRunListResponse:
        rows, total = eval_state.list_eval_runs(
            eval_type=eval_type,
            status=status,
            page=page,
            page_size=page_size,
        )
        page, page_size, _, _ = _page_bounds(page, page_size)
        return EvalRunListResponse(
            items=[_record_to_list_item(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get(
        "/api/v1/evaluations/runs/{eval_run_id}",
        response_model=EvalRunResponse,
    )
    def get_eval_run(eval_run_id: str) -> EvalRunResponse:
        record = eval_state.get_eval_run(eval_run_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "eval_run_not_found",
                    "eval_run_id": eval_run_id,
                },
            )
        return _record_to_response(record)

    @app.get("/api/v1/evaluations/compare")
    def compare_eval_runs(run_a: str, run_b: str) -> dict[str, object]:
        record_a = eval_state.get_eval_run(run_a)
        record_b = eval_state.get_eval_run(run_b)
        if record_a is None or record_b is None:
            missing = run_a if record_a is None else run_b
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "eval_run_not_found",
                    "eval_run_id": missing,
                },
            )
        return compare_reports(record_a, record_b)

    @app.post("/api/v1/inspect/{service_name}")
    def inspect_service(
        service_name: str,
        check_items: str | None = None,
    ) -> dict[str, object]:
        items = (
            [item.strip() for item in check_items.split(",") if item.strip()]
            if check_items
            else None
        )
        payload = run_inspection(service_name, check_items=items)
        write_inspection_log(payload)
        return payload

    @app.get("/api/v1/metrics/platform")
    def platform_metrics_endpoint() -> dict[str, object]:
        return snapshot_dict()

    @app.get("/api/v1/metrics/platform/timeseries")
    def platform_metrics_timeseries_endpoint() -> dict[str, object]:
        """Rolling timeseries of headline indicators for dashboard trend charts."""
        return snapshot_timeseries()

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return render_prometheus_text()

    @app.websocket("/api/v1/ws/runs/{run_id}")
    async def runs_websocket(websocket: WebSocket, run_id: str) -> None:
        """Subscribe to live events for run_id.

        Replays the in-process event history first, then streams new
        events until the client disconnects.  Errors close the socket
        with code 1011 so clients know to retry.
        """
        from fastapi import WebSocketDisconnect

        await websocket.accept()
        queue_id, queue = platform_bus.subscribe(run_id)
        try:
            while True:
                ev = await queue.get()
                await websocket.send_text(json.dumps(ev.to_dict(), ensure_ascii=False))
        except WebSocketDisconnect:
            pass
        except Exception:
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            platform_bus.unsubscribe(run_id=run_id, queue_id=queue_id)

    # ------------------------------------------------------------------ #
    # MCP-compatible tool registry (spec §3.3 / MCP tools/list)
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/mcp/tools")
    def list_mcp_tools() -> dict[str, object]:
        """Return the agent-platform tools in MCP ``tools/list`` shape.

        R21: delegates to the mcp-gateway (in-memory or HTTP) so the
        MCP shape is owned by the gateway service.  The in-memory
        client builds the shape from ``state.tools``; the HTTP client
        forwards the gateway's response verbatim.
        """
        return mcp_state.list_mcp_tools(service_name=None)

    # ------------------------------------------------------------------ #
    # R22 object-store endpoints
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/objects")
    def upload_object(
        request: Request,
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        """Upload a binary object and return its ``object_key`` + presigned URL.

        Used by the supplement flow (R20-1) and any future caller that
        needs to attach a file without inlining base64.  The key is
        ``{prefix}/{uuid}{ext}``; the prefix is fixed to
        ``uploads/<tenant>`` so multi-tenant namespaces stay clean.
        """
        from ai_employee.object_store import build_object_store

        prefix = os.getenv("OBJECT_STORE_PREFIX", "uploads")
        tenant = os.getenv("TENANT_ID", "default")
        ext = os.path.splitext(file.filename or "")[1] or ""
        key = f"{prefix}/{tenant}/{uuid.uuid4().hex}{ext}"

        content = file.file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "empty_object", "message": "no bytes received"},
            )
        try:
            store = build_object_store()
            store.put(
                key,
                content,
                content_type=file.content_type,
                metadata={"original_filename": file.filename or ""},
            )
            presigned = store.presign(key, expires=3600)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "invalid_object_key", "message": str(exc)},
            ) from exc
        return {
            "object_key": key,
            "size": len(content),
            "content_type": file.content_type,
            "presigned_url": presigned,
        }

    @app.get("/api/v1/objects/{key:path}/download")
    def download_object(key: str) -> Response:
        """Stream an object back to the caller.

        Auth-aware: requires ``X-Internal-Token`` (when configured) or
        the standard auth chain.  Streams the bytes directly so large
        PDFs / images don't buffer in memory.
        """
        from ai_employee.object_store import build_object_store

        try:
            store = build_object_store()
            meta = store.get_metadata(key)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "object_not_found", "object_key": key},
            ) from exc
        data = store.get(key)
        return Response(
            content=data,
            media_type=meta.get("content_type", "application/octet-stream"),
            headers={"Content-Length": str(len(data))},
        )

    return app


def _page_bounds(page: int, page_size: int) -> tuple[int, int, int, int]:
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))
    start = (page - 1) * page_size
    end = start + page_size
    return page, page_size, start, end


def _execute_rag_eval(golden_path: str, api_base: str | None) -> UnifiedReport:
    """Run a RAG eval (eval-service) and adapt to UnifiedReport.

    Imports are lazy so the platform app does not require the eval-service at
    import time and so tests can monkeypatch ``ai_employee.eval.runner.run``.
    """
    from ai_employee.eval import metrics as eval_metrics
    from ai_employee.eval import report as eval_report
    from ai_employee.eval import runner

    if not api_base:
        raise ValueError("api_base is required for rag evaluation")
    results = runner.run(
        golden_path=golden_path,
        api_base=api_base,
        top_ks=EVAL_TOP_KS,
    )
    metrics = eval_metrics.compute(results, EVAL_TOP_KS)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = eval_report.build_report(
        metrics,
        golden_path=golden_path,
        api_base=api_base,
        top_ks=EVAL_TOP_KS,
        ts=ts,
        thresholds={},
    )
    return to_unified_rag(metrics, report)


def _execute_rca_eval(golden_path: str) -> UnifiedReport:
    """Run an RCA replay (rca-agent) and adapt to UnifiedReport.

    Imported lazily to avoid import cycles with rca-agent.
    """
    from ai_employee.rca_agent import replay as rca_replay

    replay_result = rca_replay.run_replay_file(golden_path)
    return to_unified_rca(replay_result)


def _record_to_response(record: dict) -> EvalRunResponse:
    return EvalRunResponse(
        eval_run_id=record["eval_run_id"],
        eval_type=record["eval_type"],
        template_id=record["template_id"],
        golden_path=record["golden_path"],
        status=record["status"],
        trace_id=record["trace_id"],
        created_at=record["created_at"],
        completed_at=record.get("completed_at"),
        report=record.get("report_json") or {},
        summary=record.get("summary"),
        error=record.get("error"),
    )


def _record_to_list_item(record: dict) -> EvalRunListItem:
    return EvalRunListItem(
        eval_run_id=record["eval_run_id"],
        eval_type=record["eval_type"],
        template_id=record["template_id"],
        golden_path=record["golden_path"],
        status=record["status"],
        trace_id=record["trace_id"],
        created_at=record["created_at"],
        completed_at=record.get("completed_at"),
        summary=record.get("summary"),
        error=record.get("error"),
    )


app = create_app()
