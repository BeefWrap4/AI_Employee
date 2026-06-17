from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ai_employee.knowledge_api.internal_auth import require_internal_token


def _app(token: str) -> FastAPI:
    app = FastAPI()
    dep = require_internal_token(token)

    @app.post("/internal/chunks")
    def chunks(_: None = Depends(dep)) -> dict:
        return {"ok": True}

    @app.post("/internal/documents/{doc_id}/parse-failed")
    def failed(doc_id: str, _: None = Depends(dep)) -> dict:
        return {"doc_id": doc_id}

    return app


def test_missing_token_returns_401() -> None:
    client = TestClient(_app("secret"))
    resp = client.post("/internal/chunks", json={})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "internal_unauthorized"


def test_wrong_token_returns_401() -> None:
    client = TestClient(_app("secret"))
    resp = client.post("/internal/chunks", json={}, headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 401


def test_correct_token_passes() -> None:
    client = TestClient(_app("secret"))
    resp = client.post(
        "/internal/chunks",
        json={},
        headers={"X-Internal-Token": "secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_parse_failed_endpoint_protected() -> None:
    client = TestClient(_app("secret"))
    resp = client.post("/internal/documents/doc_001/parse-failed", json={})
    assert resp.status_code == 401
