from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.agent_platform_api.app import create_app


def test_list_agent_templates_exposes_first_three_mvp_templates() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/agent-templates")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [item["template_id"] for item in body["items"]] == [
        "knowledge_qa",
        "rca",
        "inspection",
    ]
    assert body["items"][0]["agent_name"] == "Knowledge QA Agent"
    assert body["items"][1]["requires_approval"] is True


def test_create_agent_run_returns_trace_and_can_be_fetched() -> None:
    client = TestClient(create_app())

    created = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "noc_user",
            "input": {
                "question": "How should RRC setup failures be triaged?",
                "knowledge_scopes": ["wireless", "5g"],
            },
        },
    )

    assert created.status_code == 201, created.text
    run = created.json()
    assert run["run_id"].startswith("agent_run_")
    assert run["template_id"] == "knowledge_qa"
    assert run["status"] == "completed"
    assert run["trace_id"].startswith("trace_agent_run_")
    assert run["approval_status"] == "not_required"
    assert run["node_trace"][-1]["node_name"] == "Completed"
    assert run["output"]["summary"]

    fetched = client.get(f"/api/v1/agent-runs/{run['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["trace_id"] == run["trace_id"]
    assert fetched.json()["node_trace"][0]["node_name"] == "TemplateLoaded"


def test_rca_agent_run_waits_for_review_and_list_filters_by_template() -> None:
    client = TestClient(create_app())
    client.post(
        "/api/v1/agent-runs",
        json={"template_id": "knowledge_qa", "requested_by": "noc_user", "input": {}},
    )

    created = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "ops_expert",
            "input": {"incident_id": "inc_001"},
        },
    )

    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "waiting_approval"
    assert run["approval_status"] == "pending"
    assert run["node_trace"][-1]["node_name"] == "ApprovalRequired"

    listed = client.get("/api/v1/agent-runs?template_id=rca")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["run_id"] == run["run_id"]
    assert body["items"][0]["status"] == "waiting_approval"


def test_unknown_template_rejected() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/agent-runs",
        json={"template_id": "unknown", "requested_by": "noc_user", "input": {}},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "agent_template_not_found"
