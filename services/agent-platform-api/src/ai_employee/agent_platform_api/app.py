from __future__ import annotations

from datetime import datetime, timezone

from ai_employee.agent_platform_api.eval_compare import compare_reports
from ai_employee.agent_platform_api.eval_store import EvalStore
from ai_employee.agent_platform_api.inspection import (
    run_inspection,
    write_inspection_log,
)
from ai_employee.agent_platform_api.platform_metrics import (
    metrics as platform_metrics,
    snapshot_dict,
)
from ai_employee.agent_platform_api.rate_limit import build_limiter
from ai_employee.agent_platform_api.run_store import AgentRunStore
from ai_employee.agent_platform_api.runtime import (
    TEMPLATES,
    AgentPlatformStore,
    create_run,
    decide_approval_task,
    list_templates,
    register_tool,
    resume_run_from_node,
    run_to_persist_dict,
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
    ApprovalTask,
    ApprovalTaskListResponse,
    EvalRunListItem,
    EvalRunListResponse,
    EvalRunRequest,
    EvalRunResponse,
    ToolListResponse,
    ToolRegistration,
    ToolResponse,
)
from ai_employee.common_schemas.eval import (
    UnifiedReport,
    to_unified_rag,
    to_unified_rca,
)
from ai_employee.common_schemas.tool_registry import (
    ToolRegistry as _McpToolRegistry,
)
from ai_employee.common_schemas.tool_registry import (
    ToolSpec as _McpToolSpec,
)
from ai_employee.observability import render_prometheus_text
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse

SERVICE_VERSION = "0.1.0"
EVAL_TOP_KS = [1, 3, 5]


def create_app(
    store: AgentPlatformStore | None = None,
    eval_store: EvalStore | None = None,
    run_store: AgentRunStore | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee Agent Platform API", version=SERVICE_VERSION)
    state = store or AgentPlatformStore()
    eval_state = eval_store or EvalStore()
    run_state = run_store or AgentRunStore()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "agent-platform-api",
            "status": "ok",
            "version": SERVICE_VERSION,
            "runtime": "in_memory",
        }

    @app.get("/api/v1/agent-templates", response_model=AgentTemplateListResponse)
    def get_agent_templates() -> AgentTemplateListResponse:
        items = list_templates()
        return AgentTemplateListResponse(items=items, total=len(items))

    @app.post(
        "/api/v1/agent-runs",
        response_model=AgentRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent_run(payload: AgentRunCreate) -> AgentRunResponse:
        if payload.template_id not in TEMPLATES:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "agent_template_not_found",
                    "template_id": payload.template_id,
                },
            )
        run = create_run(state, payload)
        run_state.upsert_run(run_to_persist_dict(run))
        platform_metrics().record_run(succeeded=(run.status != 'failed'))
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
        tasks = list(state.approval_tasks.values())
        if status is not None:
            tasks = [task for task in tasks if task.status == status]
        total = len(tasks)
        page, page_size, start, end = _page_bounds(page, page_size)
        return ApprovalTaskListResponse(
            items=tasks[start:end],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.post(
        "/api/v1/approval-tasks/{task_id}/decision",
        response_model=ApprovalTask,
    )
    def decide_approval(task_id: str, payload: ApprovalDecisionRequest) -> ApprovalTask:
        task = state.approval_tasks.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "approval_task_not_found", "task_id": task_id},
            )
        if task.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "approval_task_already_decided",
                    "task_id": task_id,
                    "current_status": task.status,
                },
            )
        updated_task = decide_approval_task(
            state,
            task_id=task_id,
            decision=payload.decision,
            decided_by=payload.decided_by,
            comment=payload.comment,
        )
        # Approval wait time: task.created_at → now.  Best-effort; missing
        # timestamps are skipped silently.
        try:
            from datetime import datetime as _dt

            _task = state.approval_tasks.get(task_id)
            if getattr(_task, "created_at", None):
                _created = _task.created_at
                if _created.endswith("Z"):
                    _created = _created.replace("Z", "+00:00")
                _wait = (
                    _dt.now(_dt.timezone.utc)
                    - _dt.fromisoformat(_created)
                ).total_seconds()
                if _wait >= 0:
                    platform_metrics().record_approval(_wait)
        except Exception:
            pass
        platform_metrics().record_review(accepted=(payload.decision == 'approved'))
        run = state.runs.get(task.run_id)
        if run is not None:
            run_state.upsert_run(run_to_persist_dict(run))
        return updated_task

    @app.post(
        "/api/v1/tools",
        response_model=ToolResponse,
        response_model_exclude_none=True,
        status_code=status.HTTP_201_CREATED,
    )
    def create_tool(payload: ToolRegistration) -> ToolResponse:
        if payload.tool_name in state.tools:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "tool_already_registered",
                    "tool_name": payload.tool_name,
                },
            )
        return register_tool(state, payload)

    @app.get("/api/v1/tools", response_model=ToolListResponse)
    def list_tools(
        risk_level: str | None = None,
        status: str | None = None,
        service_name: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ToolListResponse:
        tools = list(state.tools.values())
        if risk_level is not None:
            tools = [tool for tool in tools if tool.risk_level == risk_level]
        if status is not None:
            tools = [tool for tool in tools if tool.status == status]
        if service_name is not None:
            tools = [tool for tool in tools if tool.service_name == service_name]
        total = len(tools)
        page, page_size, start, end = _page_bounds(page, page_size)
        return ToolListResponse(
            items=tools[start:end],
            total=total,
            page=page,
            page_size=page_size,
        )

    # ------------------------------------------------------------------ #
    # Eval center (spec §7)
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v1/evaluations/runs",
        response_model=EvalRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_eval_run(payload: EvalRunRequest) -> EvalRunResponse:
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
        return _record_to_response(record)

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

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return render_prometheus_text()

    # ------------------------------------------------------------------ #
    # MCP-compatible tool registry (spec §3.3 / MCP tools/list)
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/mcp/tools")
    def list_mcp_tools() -> dict[str, object]:
        """Return the agent-platform tools in MCP ``tools/list`` shape.

        Built from the in-memory ``state.tools`` plus a curated set of
        well-known downstream tool names. The result is JSON-serialisable
        and follows the lightweight schema in
        ``ai_employee.common_schemas.tool_registry``.
        """
        registry = _McpToolRegistry()
        for tool in state.tools.values():
            registry.register(
                _McpToolSpec(
                    name=tool.tool_name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                    risk_level=tool.risk_level,
                    service_name=tool.service_name,
                )
            )
        return registry.to_mcp_list()

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
