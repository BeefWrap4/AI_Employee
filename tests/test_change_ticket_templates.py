"""Spec §5.5 — change_assessment + ticket_summary template coverage.

These templates (5/5 complete set: knowledge_qa, rca, inspection,
change_assessment, ticket_summary) must each be reachable via the
agent-run endpoint, return their declared output schema, and route to
the right tools.
"""

from __future__ import annotations

from typing import Any

from ai_employee.agent_platform_api.app import create_app
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(create_app())


def test_change_assessment_template_listed() -> None:
    client = _client()
    resp = client.get("/api/v1/agent-templates")
    assert resp.status_code == 200
    ids = {t["template_id"] for t in resp.json()["items"]}
    assert "change_assessment" in ids


def test_change_assessment_run_full_flow() -> None:
    client = _client()
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "change_assessment",
            "requested_by": "alice",
            "input": {
                "change_id": "CR-2026-0618-001",
                "change_type": "parameter",
                "affected_ne_ids": ["NE-001", "NE-002"],
            },
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["template_id"] == "change_assessment"
    # Approval required → status 'waiting_approval'.
    assert body["status"] == "waiting_approval"
    assert body["approval_status"] == "pending"


def test_ticket_summary_template_listed() -> None:
    client = _client()
    resp = client.get("/api/v1/agent-templates")
    assert resp.status_code == 200
    ids = {t["template_id"] for t in resp.json()["items"]}
    assert "ticket_summary" in ids


def test_ticket_summary_run_completes() -> None:
    client = _client()
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "ticket_summary",
            "requested_by": "bob",
            "input": {"ticket_id": "T-1001"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["template_id"] == "ticket_summary"
    # No approval needed → 'completed' directly.
    assert body["status"] == "completed"


def test_all_five_templates_listed() -> None:
    """Spec §5.5 mandates the 5-template set."""
    client = _client()
    resp = client.get("/api/v1/agent-templates")
    assert resp.status_code == 200
    ids = {t["template_id"] for t in resp.json()["items"]}
    expected = {
        "knowledge_qa",
        "rca",
        "inspection",
        "change_assessment",
        "ticket_summary",
    }
    assert expected.issubset(ids), f"missing templates: {expected - ids}"


def test_change_assessment_output_has_risk_level() -> None:
    """Approval gate must produce risk_level + risk_factors in output."""
    client = _client()
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "change_assessment",
            "requested_by": "alice",
            "input": {"change_id": "CR-X"},
        },
    )
    body = resp.json()
    # When a run is awaiting approval, the draft output is still set.
    assert "output" in body
    out: dict[str, Any] = body["output"]
    assert "risk_level" in out
    assert "risk_factors" in out
