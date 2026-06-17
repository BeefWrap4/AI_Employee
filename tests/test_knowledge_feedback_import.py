from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_employee.rca_agent.app import create_app


def _alarm(alarm_id: str, site_id: str, alarm_code: str = "LINK_DEGRADE") -> dict:
    return {
        "alarm_id": alarm_id,
        "alarm_code": alarm_code,
        "alarm_name": alarm_code.replace("_", " ").title(),
        "vendor": "huawei",
        "site_id": site_id,
        "cell_id": f"{site_id}-CELL",
        "ne_id": f"{site_id}-NE",
        "severity": "critical",
        "start_time": "2026-06-17T10:00:00+08:00",
        "raw_payload": {},
    }


def _create_run(client: TestClient, alarm_id: str, site_id: str) -> dict:
    response = client.post(
        "/api/v1/rca/runs",
        json={"alarms": [_alarm(alarm_id, site_id)], "require_human_review": True},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _accept(client: TestClient, report_id: str) -> dict:
    response = client.post(
        f"/api/v1/rca/reports/{report_id}/review",
        json={"decision": "accepted", "final_root_cause": "confirmed link degradation"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve_candidate(client: TestClient, candidate_id: str) -> dict:
    response = client.post(
        f"/api/v1/candidate-knowledge/{candidate_id}/review",
        json={"decision": "approved", "reviewer": "expert_01", "comment": "ok"},
    )
    assert response.status_code == 200, response.text
    return response.json()


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _make_candidate_approved(client: TestClient, alarm_id: str, site_id: str) -> str:
    created = _create_run(client, alarm_id, site_id)
    _accept(client, created["report_id"])
    candidate_id = client.get("/api/v1/candidate-knowledge").json()["items"][0]["candidate_id"]
    _approve_candidate(client, candidate_id)
    return candidate_id


def test_import_candidate_success_updates_imported_doc_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app())
    candidate_id = _make_candidate_approved(client, "a_001", "SITE-001")

    captured: dict = {}

    def fake_post(url: str, files=None, data=None, timeout=None) -> _FakeResponse:
        captured["url"] = url
        captured["files"] = files
        captured["data"] = data
        return _FakeResponse(202, {"doc_id": "doc_abc123", "title": "t", "status": "uploaded"})

    monkeypatch.setattr(httpx, "post", fake_post)

    response = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_doc_id"] == "doc_abc123"
    assert body["review_status"] == "approved"
    # multipart request shape per spec §6.3
    assert "/api/v1/documents" in captured["url"]
    assert captured["data"]["title"] == body["title"]
    assert captured["data"]["version"] == "v1"
    assert captured["data"]["mime_type"] == "text/markdown"
    assert '"rca_feedback"' in captured["data"]["acl_tags_json"]
    assert "rca_feedback" in captured["data"]["metadata_json"]
    assert "SITE" in captured["data"]["metadata_json"] or "inc_" in captured["data"]["metadata_json"]
    file_tuple = captured["files"]["file"]
    assert file_tuple[0].endswith(".md")
    assert file_tuple[2] == "text/markdown"


def test_import_candidate_not_approved_returns_409() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_002", "SITE-002")
    _accept(client, created["report_id"])
    candidate_id = client.get("/api/v1/candidate-knowledge").json()["items"][0]["candidate_id"]
    # candidate is still pending (not approved)

    response = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "not_approved"
    assert response.json()["detail"]["current_status"] == "pending"


def test_import_candidate_rejected_returns_409_not_approved() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_003", "SITE-003")
    _accept(client, created["report_id"])
    candidate_id = client.get("/api/v1/candidate-knowledge").json()["items"][0]["candidate_id"]
    reject = client.post(
        f"/api/v1/candidate-knowledge/{candidate_id}/review",
        json={"decision": "rejected", "reviewer": "expert_02"},
    )
    assert reject.status_code == 200

    response = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "not_approved"


def test_import_candidate_already_imported_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app())
    candidate_id = _make_candidate_approved(client, "a_004", "SITE-004")

    def fake_post(url: str, files=None, data=None, timeout=None) -> _FakeResponse:
        return _FakeResponse(202, {"doc_id": "doc_first", "title": "t", "status": "uploaded"})

    monkeypatch.setattr(httpx, "post", fake_post)
    first = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")
    assert first.status_code == 200

    second = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "already_imported"
    assert second.json()["detail"]["imported_doc_id"] == "doc_first"


def test_import_candidate_knowledge_api_unreachable_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app())
    candidate_id = _make_candidate_approved(client, "a_005", "SITE-005")

    def fake_post(url: str, files=None, data=None, timeout=None) -> _FakeResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    response = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "knowledge_api_unavailable"
    # candidate stays approved and not imported (retryable)
    detail = client.get(f"/api/v1/candidate-knowledge/{candidate_id}").json()
    assert detail["review_status"] == "approved"
    assert detail["imported_doc_id"] is None


def test_import_candidate_unknown_id_returns_404() -> None:
    client = TestClient(create_app())
    response = client.post("/api/v1/candidate-knowledge/ck_unknown/import")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "candidate_not_found"


def test_import_uses_knowledge_api_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", "http://example-kb:9999")
    client = TestClient(create_app())
    candidate_id = _make_candidate_approved(client, "a_006", "SITE-006")

    captured: dict = {}

    def fake_post(url: str, files=None, data=None, timeout=None) -> _FakeResponse:
        captured["url"] = url
        return _FakeResponse(202, {"doc_id": "doc_env", "title": "t", "status": "uploaded"})

    monkeypatch.setattr(httpx, "post", fake_post)
    response = client.post(f"/api/v1/candidate-knowledge/{candidate_id}/import")
    assert response.status_code == 200
    assert captured["url"].startswith("http://example-kb:9999/api/v1/documents")
