"""Inspection agent API endpoint tests."""
from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.agent_platform_api.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app())


def test_inspect_endpoint_returns_findings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "inspections"))
    client = _client(tmp_path)
    resp = client.post("/api/v1/inspect/knowledge-api")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target"] == "knowledge-api"
    assert body["risk_level"] == "read_only"
    assert body["findings"]
    assert (tmp_path / "inspections" / "knowledge-api.jsonl").is_file()


def test_inspect_endpoint_supports_check_items_filter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "inspections"))
    client = _client(tmp_path)
    resp = client.post(
        "/api/v1/inspect/rca-agent?check_items=service_health,missing_check",
    )
    assert resp.status_code == 200
    findings = resp.json()["findings"]
    assert all(f["check_name"] == "service_health" for f in findings)
    assert findings  # non-empty because service_health is in the filter
