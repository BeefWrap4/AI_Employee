"""Pluggable approval-service client (R21 service isolation).

Spec §9 moves approval tasks into a standalone ``approval-service``.
The agent-platform keeps its existing endpoint contracts (consumers are
unaware) but delegates the task state machine to the service when
``APPROVAL_SERVICE_URL`` is set; otherwise it falls back to the
in-memory store (backward compat / tests).

This module defines the :class:`ApprovalServiceClient` Protocol plus
three implementations:

* :class:`InMemoryApprovalServiceClient` — wraps the existing runtime
  functions against the platform's in-memory ``AgentPlatformStore``.
  This is the default (env unset) and the test path.  It performs the
  task transition **and** the run side-effect together, exactly as the
  legacy ``decide_approval_task`` did.
* :class:`HttpApprovalServiceClient` — delegates the task transition to
  a remote ``approval-service`` over HTTP.  It returns the updated
  task; the platform applies the run side-effect locally
  (``apply_decision_run_effect``) because the service does not own runs.
* :class:`FakeApprovalServiceClient` — in-process fake for tests that
  want to assert the delegation surface without a socket.

Run side-effects (complete / fail the run, append node trace) always
live in the platform.  :func:`apply_decision_run_effect` is the single
source of truth used by the HTTP path; the in-memory path reuses the
existing combined ``runtime.decide_approval_task`` for zero regression.
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

import httpx
from ai_employee.agent_platform_api import runtime
from ai_employee.agent_platform_api.schemas import ApprovalTask, ToolResponse


@runtime_checkable
class ApprovalServiceClient(Protocol):
    """Approval-task lifecycle delegation surface.

    Each method returns the updated :class:`ApprovalTask` (the
    agent-platform applies run side-effects separately on decision).

    Implementations set ``applies_run_side_effects`` to signal whether
    ``decide`` already mutated the platform's run store (in-memory path)
    or whether the platform must apply the run effect itself (HTTP path).
    """

    applies_run_side_effects: bool

    def list_tasks(self, *, status: str | None, page: int, page_size: int) -> dict[str, Any]: ...

    def get_task(self, task_id: str) -> ApprovalTask | None: ...

    def create_task(self, task: ApprovalTask) -> ApprovalTask: ...

    def decide(
        self,
        *,
        task_id: str,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> ApprovalTask: ...

    def request_supplement(
        self,
        *,
        task_id: str,
        note: str,
        attachments: list[dict[str, Any]],
        requested_by: str,
    ) -> ApprovalTask: ...

    def resolve_supplement(
        self,
        *,
        task_id: str,
        attachments: list[dict[str, Any]],
        note: str | None,
        resolved_by: str,
    ) -> ApprovalTask: ...

    def transfer(
        self,
        *,
        task_id: str,
        new_approver: str,
        reason: str,
        transferred_by: str,
        is_admin: bool,
    ) -> ApprovalTask: ...

    def escalate(
        self,
        *,
        task_id: str,
        escalated_to: str | None,
        reason: str | None,
        escalated_by: str | None,
    ) -> ApprovalTask: ...


# --------------------------------------------------------------------------- #
# McpGatewayClient — R21 tool gateway delegation
# --------------------------------------------------------------------------- #


@runtime_checkable
class McpGatewayClient(Protocol):
    """Tool-registry / discovery / invocation delegation surface.

    Mirrors the platform's ``/api/v1/tools`` + ``/api/v1/mcp/tools``
    endpoints but speaks the gateway's contract (id is ``tool_name``;
    the gateway uses ``name``).  The HTTP implementation translates
    field names when serialising; the InMemory implementation reuses
    the platform's ``runtime.register_tool`` for zero regression.
    """

    def list_tools(
        self,
        *,
        risk_level: str | None,
        status: str | None,
        service_name: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]: ...

    def list_mcp_tools(self, *, service_name: str | None) -> dict[str, Any]: ...

    def get_tool(self, tool_name: str) -> ToolResponse | None: ...

    def register(self, payload: dict[str, Any]) -> ToolResponse: ...


class _McpError(Exception):
    """Carries the upstream status code + body for re-mapping."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"mcp-gateway returned {status_code}")
        self.status_code = status_code
        self.body = body


class InMemoryMcpGatewayClient:
    """Delegates to the existing ``runtime.register_tool`` against the store.

    Used when ``MCP_GATEWAY_URL`` is unset.  Preserves the legacy
    in-memory tool dict so all existing tests keep passing.
    """

    def __init__(self, store: runtime.AgentPlatformStore | None = None) -> None:
        self._store: runtime.AgentPlatformStore | None = store

    def bind(self, store: runtime.AgentPlatformStore) -> None:
        self._store = store

    @property
    def store(self) -> runtime.AgentPlatformStore:
        if self._store is None:
            raise RuntimeError("InMemoryMcpGatewayClient.store not bound")
        return self._store

    def list_tools(
        self,
        *,
        risk_level: str | None,
        status: str | None,
        service_name: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        tools = list(self.store.tools.values())
        if risk_level is not None:
            tools = [t for t in tools if t.risk_level == risk_level]
        if status is not None:
            tools = [t for t in tools if t.status == status]
        if service_name is not None:
            tools = [t for t in tools if t.service_name == service_name]
        page, page_size, start, end = _page_bounds(page, page_size)
        return {
            "items": [t.model_dump() for t in tools[start:end]],
            "total": len(tools),
            "page": page,
            "page_size": page_size,
        }

    def list_mcp_tools(self, *, service_name: str | None) -> dict[str, Any]:
        tools = list(self.store.tools.values())
        if service_name is not None:
            tools = [t for t in tools if t.service_name == service_name]
        return {
            "tools": [
                {
                    "name": t.tool_name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                    "metadata": {
                        "risk_level": t.risk_level,
                        "service_name": t.service_name,
                    },
                }
                for t in tools
            ],
        }

    def get_tool(self, tool_name: str) -> ToolResponse | None:
        return self.store.tools.get(tool_name)

    def register(self, payload: dict[str, Any]) -> ToolResponse:
        from ai_employee.agent_platform_api.schemas import ToolRegistration

        return runtime.register_tool(self.store, ToolRegistration(**payload))


def _page_bounds(page: int, page_size: int) -> tuple[int, int, int, int]:
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))
    start = (page - 1) * page_size
    end = start + page_size
    return page, page_size, start, end


class HttpMcpGatewayClient:
    """Delegates tool calls to a remote mcp-gateway over HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token or os.getenv("INTERNAL_TOKEN")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Internal-Token"] = self.token
        return h

    def _post(self, path: str, json: dict[str, Any]) -> httpx.Response:  # pragma: no cover
        return httpx.post(
            self.base_url + path, json=json, headers=self._headers(), timeout=self.timeout
        )

    def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:  # pragma: no cover
        return httpx.get(
            self.base_url + path, params=params, headers=self._headers(), timeout=self.timeout
        )

    def _check(self, resp: Any) -> dict[str, Any]:
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"detail": {"error_code": "mcp_gateway_error", "message": resp.text}}
            raise _McpError(resp.status_code, body)
        try:
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _to_gateway_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Translate platform field name ``tool_name`` → gateway ``name``.

        Drops fields set to ``None`` (e.g. ``timeout_ms``) so the
        gateway's Pydantic model (which expects plain ints) doesn't
        reject the request.
        """
        out = {k: v for k, v in payload.items() if v is not None}
        if "tool_name" in out and "name" not in out:
            out["name"] = out.pop("tool_name")
        out.pop("status", None)
        out.pop("health_status", None)
        return out

    def list_tools(
        self,
        *,
        risk_level: str | None,
        status: str | None,
        service_name: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        # The gateway doesn't currently expose the platform's rich
        # filter set (risk_level / status), so we filter the gateway's
        # response client-side.  ``service_name`` IS a gateway query
        # param.
        params: dict[str, Any] = {"service_name": service_name} if service_name else {}
        resp = self._get("/api/v1/tools", params=params)
        body = self._check(resp)
        tools = body.get("tools", [])
        # The gateway returns MCP shape; adapt to the platform
        # ``ToolListResponse`` shape.
        items = [
            {
                "tool_name": t.get("name"),
                "description": t.get("description", ""),
                "service_name": (t.get("metadata") or {}).get("service_name"),
                "risk_level": (t.get("metadata") or {}).get("risk_level", "read_only"),
                "status": "active",
                "input_schema": t.get("inputSchema") or {},
                "output_schema": {},
                "health_status": "unknown",
            }
            for t in tools
        ]
        if risk_level is not None:
            items = [i for i in items if i["risk_level"] == risk_level]
        if status is not None:
            items = [i for i in items if i["status"] == status]
        page_n = max(1, int(page))
        page_size_n = max(1, min(200, int(page_size)))
        start = (page_n - 1) * page_size_n
        end = start + page_size_n
        return {
            "items": items[start:end],
            "total": len(items),
            "page": page_n,
            "page_size": page_size_n,
        }

    def list_mcp_tools(self, *, service_name: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"service_name": service_name} if service_name else {}
        resp = self._get("/api/v1/tools", params=params)
        return self._check(resp)

    def get_tool(self, tool_name: str) -> ToolResponse | None:
        resp = self._get(f"/api/v1/tools/{tool_name}")
        if resp.status_code == 404:
            return None
        body = self._check(resp)
        meta = body.get("metadata") or {}
        return ToolResponse(
            tool_name=body.get("name", tool_name),
            service_name=meta.get("service_name", ""),
            description=body.get("description", ""),
            input_schema=body.get("inputSchema") or {},
            output_schema={},
            risk_level=meta.get("risk_level", "read_only"),
            status="active",
            health_status="unknown",
        )

    def register(self, payload: dict[str, Any]) -> ToolResponse:
        gw_payload = self._to_gateway_payload(payload)
        resp = self._post("/api/v1/tools", json=gw_payload)
        self._check(resp)
        # Echo back the platform-shaped ToolResponse.
        return ToolResponse(
            tool_name=payload["tool_name"],
            service_name=payload.get("service_name", ""),
            description=payload.get("description", ""),
            input_schema=payload.get("input_schema") or {},
            output_schema=payload.get("output_schema") or {},
            risk_level=payload.get("risk_level", "read_only"),
            status=payload.get("status", "active"),
            health_status="unknown",
        )


class FakeMcpGatewayClient:
    """In-process fake that records every call and mutates a local dict."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolResponse] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, dict(kwargs)))

    def seed(self, tool: ToolResponse) -> None:
        self._tools[tool.tool_name] = tool

    def list_tools(self, **_kwargs: Any) -> dict[str, Any]:
        self._record("list_tools", **_kwargs)
        return {
            "items": [t.model_dump() for t in self._tools.values()],
            "total": len(self._tools),
            "page": 1,
            "page_size": 50,
        }

    def list_mcp_tools(self, **_kwargs: Any) -> dict[str, Any]:
        self._record("list_mcp_tools", **_kwargs)
        return {
            "tools": [
                {
                    "name": t.tool_name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                    "metadata": {"risk_level": t.risk_level, "service_name": t.service_name},
                }
                for t in self._tools.values()
            ],
        }

    def get_tool(self, tool_name: str) -> ToolResponse | None:
        self._record("get_tool", tool_name=tool_name)
        return self._tools.get(tool_name)

    def register(self, payload: dict[str, Any]) -> ToolResponse:
        self._record("register", **payload)
        tool = ToolResponse(
            tool_name=payload["tool_name"],
            service_name=payload.get("service_name", ""),
            description=payload.get("description", ""),
            input_schema=payload.get("input_schema") or {},
            output_schema=payload.get("output_schema") or {},
            risk_level=payload.get("risk_level", "read_only"),
            status=payload.get("status", "active"),
            health_status="unknown",
        )
        self._tools[tool.tool_name] = tool
        return tool


def build_mcp_client(
    store: runtime.AgentPlatformStore | None = None,
) -> McpGatewayClient:
    """Pick the MCP gateway client from env.

    ``MCP_GATEWAY_URL`` set  → :class:`HttpMcpGatewayClient`.
    unset                     → :class:`InMemoryMcpGatewayClient`
                               (bound to ``store`` lazily by the app).
    """
    url = os.getenv("MCP_GATEWAY_URL", "").strip()
    if url:
        return HttpMcpGatewayClient(url)
    return InMemoryMcpGatewayClient(store)


# --------------------------------------------------------------------------- #
# Run side-effect (owned by the platform; shared by the HTTP path)
# --------------------------------------------------------------------------- #


def apply_decision_run_effect(
    store: runtime.AgentPlatformStore,
    *,
    task: ApprovalTask,
    decision: str,
    comment: str | None,
    decided_by: str,
) -> None:
    """Apply the run side-effect of an approval decision.

    Mirrors the run mutation inside ``runtime.decide_approval_task`` so
    the HTTP delegation path (where the service owns only the task)
    produces the same run outcome as the in-memory path.  This does NOT
    re-transition the task — the caller already has the final task from
    the service / runtime.  Best-effort: missing runs are skipped.
    """
    run = store.runs.get(task.run_id)
    if run is None:
        return
    approved = decision == "approved"
    updated_run = run.model_copy(
        update={
            "status": "completed" if approved else "failed",
            "approval_status": decision,
            "node_trace": [
                *run.node_trace,
                runtime.NodeTrace(
                    node_name="ApprovalApproved" if approved else "ApprovalRejected",
                    status="completed" if approved else "failed",
                    detail=comment or f"Approval {decision} by {decided_by}.",
                ),
            ],
            "tool_calls": [
                tool.model_copy(update={"status": "completed" if approved else "skipped"})
                for tool in run.tool_calls
            ],
            "output": runtime._approved_output(run.output, approved),
        }
    )
    store.runs[run.run_id] = updated_run


# --------------------------------------------------------------------------- #
# In-memory client (default / fallback)
# --------------------------------------------------------------------------- #


class InMemoryApprovalServiceClient:
    """Delegates to the existing runtime functions against the store.

    Used when ``APPROVAL_SERVICE_URL`` is unset.  Performs both the
    task transition and the run side-effect (legacy behaviour) so the
    platform's in-memory mode is unchanged.
    """

    def __init__(self, store: runtime.AgentPlatformStore | None = None) -> None:
        # Late-bound: the platform passes its store via ``bind`` so a
        # single client instance shares the app's store.
        self._store: runtime.AgentPlatformStore | None = store

    applies_run_side_effects = True

    def bind(self, store: runtime.AgentPlatformStore) -> None:
        self._store = store

    @property
    def store(self) -> runtime.AgentPlatformStore:
        if self._store is None:
            raise RuntimeError("InMemoryApprovalServiceClient.store not bound")
        return self._store

    def list_tasks(self, *, status: str | None, page: int, page_size: int) -> dict[str, Any]:
        tasks = list(self.store.approval_tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        total = len(tasks)
        page_n = max(1, int(page))
        page_size_n = max(1, min(200, int(page_size)))
        start = (page_n - 1) * page_size_n
        end = start + page_size_n
        return {
            "items": [t.model_dump() for t in tasks[start:end]],
            "total": total,
            "page": page_n,
            "page_size": page_size_n,
        }

    def get_task(self, task_id: str) -> ApprovalTask | None:
        return self.store.approval_tasks.get(task_id)

    def create_task(self, task: ApprovalTask) -> ApprovalTask:
        # create_run already inserted the task into the local store; the
        # in-memory client is a no-op so the platform owns the task.
        self.store.approval_tasks[task.task_id] = task
        return task

    def decide(
        self,
        *,
        task_id: str,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> ApprovalTask:
        # Combined task + run side-effect (legacy path).
        return runtime.decide_approval_task(
            self.store,
            task_id=task_id,
            decision=decision,
            decided_by=decided_by,
            comment=comment,
        )

    def request_supplement(
        self,
        *,
        task_id: str,
        note: str,
        attachments: list[dict[str, Any]],
        requested_by: str,
    ) -> ApprovalTask:
        return runtime.request_supplement_governance(
            self.store,
            task_id=task_id,
            note=note,
            attachments=attachments,
            requested_by=requested_by,
        )

    def resolve_supplement(
        self,
        *,
        task_id: str,
        attachments: list[dict[str, Any]],
        note: str | None,
        resolved_by: str,
    ) -> ApprovalTask:
        return runtime.resolve_supplement_governance(
            self.store,
            task_id=task_id,
            attachments=attachments,
            note=note,
            resolved_by=resolved_by,
        )

    def transfer(
        self,
        *,
        task_id: str,
        new_approver: str,
        reason: str,
        transferred_by: str,
        is_admin: bool,
    ) -> ApprovalTask:
        return runtime.transfer_approval(
            self.store,
            task_id=task_id,
            new_approver=new_approver,
            reason=reason,
            transferred_by=transferred_by,
            is_admin=is_admin,
        )

    def escalate(
        self,
        *,
        task_id: str,
        escalated_to: str | None,
        reason: str | None,
        escalated_by: str | None,
    ) -> ApprovalTask:
        return runtime.escalate_approval(
            self.store,
            task_id=task_id,
            escalated_to=escalated_to,
            reason=reason,
            escalated_by=escalated_by,
        )


# --------------------------------------------------------------------------- #
# HTTP client (production delegation)
# --------------------------------------------------------------------------- #


class _ApprovalError(Exception):
    """Carries the upstream status code + body for re-mapping."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"approval-service returned {status_code}")
        self.status_code = status_code
        self.body = body


class HttpApprovalServiceClient:
    """Delegates approval transitions to a remote approval-service.

    The service owns the task state machine; this client shuttles JSON.
    Run side-effects are applied by the platform after ``decide``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token or os.getenv("INTERNAL_TOKEN")

    applies_run_side_effects = False

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Internal-Token"] = self.token
        return h

    # The two transport methods are overridable in tests (monkeypatched
    # to route through a TestClient) so the HTTP path can be exercised
    # hermetically without opening a socket.
    def _post(self, path: str, json: dict[str, Any]) -> httpx.Response:  # pragma: no cover
        return httpx.post(
            self.base_url + path, json=json, headers=self._headers(), timeout=self.timeout
        )

    def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:  # pragma: no cover
        return httpx.get(
            self.base_url + path, params=params, headers=self._headers(), timeout=self.timeout
        )

    def _check(self, resp: Any) -> dict[str, Any]:
        status_code = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {"detail": {"error_code": "approval_service_error", "message": resp.text}}
        if status_code >= 400:
            raise _ApprovalError(status_code, body)
        return body

    def list_tasks(self, *, status: str | None, page: int, page_size: int) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status is not None:
            params["status"] = status
        resp = self._get("/api/v1/approval-tasks", params=params)
        return self._check(resp)

    def get_task(self, task_id: str) -> ApprovalTask | None:
        resp = self._get(f"/api/v1/approval-tasks/{task_id}")
        if resp.status_code == 404:
            return None
        return ApprovalTask(**self._check(resp))

    def create_task(self, task: ApprovalTask) -> ApprovalTask:
        resp = self._post(
            "/api/v1/approval-tasks",
            json={
                "task_id": task.task_id,
                "run_id": task.run_id,
                "template_id": task.template_id,
                "requested_by": task.requested_by,
                "risk_level": task.risk_level,
                "reason": task.reason,
                "current_approver": task.current_approver,
                "created_at": task.created_at,
            },
        )
        return ApprovalTask(**self._check(resp))

    def decide(
        self,
        *,
        task_id: str,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> ApprovalTask:
        resp = self._post(
            f"/api/v1/approval-tasks/{task_id}/decision",
            json={"decision": decision, "decided_by": decided_by, "comment": comment},
        )
        return ApprovalTask(**self._check(resp))

    def request_supplement(
        self,
        *,
        task_id: str,
        note: str,
        attachments: list[dict[str, Any]],
        requested_by: str,
    ) -> ApprovalTask:
        resp = self._post(
            f"/api/v1/approvals/{task_id}/supplement",
            json={"note": note, "attachments": attachments, "requested_by": requested_by},
        )
        return ApprovalTask(**self._check(resp))

    def resolve_supplement(
        self,
        *,
        task_id: str,
        attachments: list[dict[str, Any]],
        note: str | None,
        resolved_by: str,
    ) -> ApprovalTask:
        resp = self._post(
            f"/api/v1/approvals/{task_id}/supplement/resolve",
            json={"attachments": attachments, "note": note, "resolved_by": resolved_by},
        )
        return ApprovalTask(**self._check(resp))

    def transfer(
        self,
        *,
        task_id: str,
        new_approver: str,
        reason: str,
        transferred_by: str,
        is_admin: bool,
    ) -> ApprovalTask:
        resp = self._post(
            f"/api/v1/approvals/{task_id}/transfer",
            json={
                "new_approver": new_approver,
                "reason": reason,
                "transferred_by": transferred_by,
                "is_admin": is_admin,
            },
        )
        return ApprovalTask(**self._check(resp))

    def escalate(
        self,
        *,
        task_id: str,
        escalated_to: str | None,
        reason: str | None,
        escalated_by: str | None,
    ) -> ApprovalTask:
        resp = self._post(
            f"/api/v1/approvals/{task_id}/escalate",
            json={"escalated_to": escalated_to, "reason": reason, "escalated_by": escalated_by},
        )
        return ApprovalTask(**self._check(resp))


# --------------------------------------------------------------------------- #
# Fake client (test double)
# --------------------------------------------------------------------------- #


class FakeApprovalServiceClient:
    """In-process fake that records every call and mutates a local dict.

    Useful for tests that assert the platform's delegation surface and
    run side-effects without spinning up the approval-service.  The
    platform still applies the run side-effect on ``decide``.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, ApprovalTask] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    applies_run_side_effects = False

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, dict(kwargs)))

    def seed(self, task: ApprovalTask) -> None:
        self._tasks[task.task_id] = task

    def list_tasks(self, *, status: str | None, page: int, page_size: int) -> dict[str, Any]:
        self._record("list_tasks", status=status, page=page, page_size=page_size)
        tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return {
            "items": [t.model_dump() for t in tasks],
            "total": len(tasks),
            "page": max(1, int(page)),
            "page_size": max(1, min(200, int(page_size))),
        }

    def get_task(self, task_id: str) -> ApprovalTask | None:
        self._record("get_task", task_id=task_id)
        return self._tasks.get(task_id)

    def create_task(self, task: ApprovalTask) -> ApprovalTask:
        self._record("create_task", task_id=task.task_id)
        self._tasks[task.task_id] = task
        return task

    def decide(
        self,
        *,
        task_id: str,
        decision: str,
        decided_by: str,
        comment: str | None,
    ) -> ApprovalTask:
        self._record("decide", task_id=task_id, decision=decision, decided_by=decided_by)
        task = self._tasks.get(task_id)
        if task is None:
            from ai_employee.agent_platform_api.runtime import ApprovalTaskNotFound

            raise ApprovalTaskNotFound(task_id)
        updated = task.model_copy(
            update={"status": decision, "decided_by": decided_by, "comment": comment}
        )
        self._tasks[task_id] = updated
        return updated

    def request_supplement(
        self, **kwargs: Any
    ) -> ApprovalTask:  # pragma: no cover - not used in tests
        self._record("request_supplement", **kwargs)
        raise NotImplementedError

    def resolve_supplement(
        self, **kwargs: Any
    ) -> ApprovalTask:  # pragma: no cover - not used in tests
        self._record("resolve_supplement", **kwargs)
        raise NotImplementedError

    def transfer(self, **kwargs: Any) -> ApprovalTask:  # pragma: no cover - not used in tests
        self._record("transfer", **kwargs)
        raise NotImplementedError

    def escalate(self, **kwargs: Any) -> ApprovalTask:  # pragma: no cover - not used in tests
        self._record("escalate", **kwargs)
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_approval_client(
    store: runtime.AgentPlatformStore | None = None,
) -> ApprovalServiceClient:
    """Pick the approval client from env.

    ``APPROVAL_SERVICE_URL`` set  → :class:`HttpApprovalServiceClient`.
    unset                         → :class:`InMemoryApprovalServiceClient`
                                   (bound to ``store`` lazily by the app).
    """
    url = os.getenv("APPROVAL_SERVICE_URL", "").strip()
    if url:
        return HttpApprovalServiceClient(url)
    return InMemoryApprovalServiceClient(store)


__all__ = [
    "ApprovalServiceClient",
    "FakeApprovalServiceClient",
    "HttpApprovalServiceClient",
    "InMemoryApprovalServiceClient",
    "apply_decision_run_effect",
    "build_approval_client",
]
