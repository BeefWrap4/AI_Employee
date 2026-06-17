from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from ai_employee.agent_platform_api.runtime import (
    AgentPlatformStore,
    TEMPLATES,
    create_run,
    decide_approval_task,
    list_templates,
    register_tool,
)
from ai_employee.agent_platform_api.schemas import (
    AgentRunCreate,
    AgentRunListResponse,
    AgentRunResponse,
    AgentRunSummary,
    AgentRunTraceResponse,
    AgentTemplateListResponse,
    ApprovalDecisionRequest,
    ApprovalTask,
    ApprovalTaskListResponse,
    ToolListResponse,
    ToolRegistration,
    ToolResponse,
)

SERVICE_VERSION = "0.1.0"


def create_app(store: AgentPlatformStore | None = None) -> FastAPI:
    app = FastAPI(title="AI Employee Agent Platform API", version=SERVICE_VERSION)
    state = store or AgentPlatformStore()

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
        return create_run(state, payload)

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
        approval_tasks = [
            task for task in state.approval_tasks.values() if task.run_id == run_id
        ]
        registered_tools = [
            tool
            for tool in state.tools.values()
            if tool.tool_name in template.tool_names
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
    def decide_approval(
        task_id: str, payload: ApprovalDecisionRequest
    ) -> ApprovalTask:
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
        return decide_approval_task(
            state,
            task_id=task_id,
            decision=payload.decision,
            decided_by=payload.decided_by,
            comment=payload.comment,
        )

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

    return app


def _page_bounds(page: int, page_size: int) -> tuple[int, int, int, int]:
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))
    start = (page - 1) * page_size
    end = start + page_size
    return page, page_size, start, end


app = create_app()
