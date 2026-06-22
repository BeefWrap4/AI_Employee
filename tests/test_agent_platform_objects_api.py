"""Tests for the R22 object-store HTTP endpoints on agent-platform-api.

* ``POST /api/v1/objects`` accepts a multipart upload and returns
  ``object_key`` + ``presigned_url``.
* ``GET  /api/v1/objects/{key}/download`` streams the bytes back.
* The LocalFs backend writes under ``OBJECT_STORE_LOCAL_ROOT``; with
  no OBJECT_STORE_URL the upload URL is a same-origin /api/v1/objects
  link.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def platform_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Spin up the platform app with a LocalFs object store under tmp_path."""
    monkeypatch.delenv("OBJECT_STORE_URL", raising=False)
    monkeypatch.delenv("OBJECT_STORE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("OBJECT_STORE_SECRET_KEY", raising=False)
    monkeypatch.delenv("OBJECT_STORE_BUCKET", raising=False)
    monkeypatch.setenv("OBJECT_STORE_LOCAL_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "uploads")
    # Keep the platform app importable from the worktree.
    from ai_employee.agent_platform_api.app import create_app

    app = create_app()
    return TestClient(app)


def test_upload_object_returns_key_and_presigned_url(platform_client: TestClient) -> None:
    resp = platform_client.post(
        "/api/v1/objects",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object_key"].startswith("uploads/")
    assert body["size"] == len(b"hello world")
    assert body["content_type"] == "text/plain"
    assert "presigned_url" in body


def test_download_object_roundtrip(platform_client: TestClient) -> None:
    upload = platform_client.post(
        "/api/v1/objects",
        files={"file": ("doc.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
    )
    key = upload.json()["object_key"]
    dl = platform_client.get(f"/api/v1/objects/{key}/download")
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 fake bytes"
    assert dl.headers["content-type"].startswith("application/pdf")


def test_download_missing_object_returns_404(platform_client: TestClient) -> None:
    resp = platform_client.get("/api/v1/objects/uploads/missing.bin/download")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "object_not_found"


def test_upload_empty_object_returns_400(platform_client: TestClient) -> None:
    resp = platform_client.post(
        "/api/v1/objects",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "empty_object"
