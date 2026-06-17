from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.agent_platform_api.app import create_app


def test_agent_run_trace_aggregates_run_template_tools_and_approvals() -> None:
    client = TestClient(create_app())
    tool = client.post(
        "/api/v1/tools",
        json={
            "tool_name": "rca-agent.runs.create",
            "service_name": "rca-agent",
            "description": "Create RCA run",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "approval_required",
            "status": "active",
        },
    )
    assert tool.status_code == 201, tool.text
    created = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "ops_expert",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    task = client.get("/api/v1/approval-tasks").json()["items"][0]
    client.post(
        f"/api/v1/approval-tasks/{task['task_id']}/decision",
        json={"decision": "approved", "decided_by": "shift_lead"},
    )

    trace = client.get(f"/api/v1/agent-runs/{run_id}/trace")

    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert body["run"]["run_id"] == run_id
    assert body["template"]["template_id"] == "rca"
    assert body["template"]["requires_approval"] is True
    assert [node["node_name"] for node in body["node_trace"]][-1] == "ApprovalApproved"
    assert body["approval_tasks"][0]["status"] == "approved"
    assert body["tool_calls"][0]["tool_name"] == "rca-agent.runs.create"
    assert body["registered_tools"][0]["tool_name"] == "rca-agent.runs.create"


def test_agent_run_trace_404_for_unknown_run() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/agent-runs/agent_run_missing/trace")

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "agent_run_not_found"
