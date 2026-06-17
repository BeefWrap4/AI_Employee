from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_employee.ingestion_worker.app import create_app


def _write(tmp_path: Path, name: str, content: str) -> str:
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    path = raw / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _set_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(tmp_path))


def test_health_reports_stub_provider() -> None:
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "ingestion-worker"
    assert body["status"] == "ok"
    assert body["embedding_provider"] == "stub"


def test_parse_markdown_returns_chunks_and_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_data_dir(monkeypatch, tmp_path)
    file_path = _write(
        tmp_path,
        "doc_001.md",
        "# 接入排障\n## RRC\n先检查告警和 KPI。\n",
    )
    client = TestClient(create_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_001",
            "file_path": file_path,
            "mime_type": "text/markdown",
            "metadata": {"network_type": "5g"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == "doc_001"
    assert body["embedding_model"] == "stub"
    assert len(body["chunks"]) >= 1
    assert len(body["embeddings"]) == len(body["chunks"])
    assert all(len(vec) == 8 for vec in body["embeddings"])
    assert body["chunks"][0]["chunk_id"].startswith("chunk_doc_001_")


def test_parse_unsupported_mime_returns_415(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_data_dir(monkeypatch, tmp_path)
    file_path = _write(tmp_path, "doc.pdf", "%PDF-1.4 fake")
    client = TestClient(create_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_002",
            "file_path": file_path,
            "mime_type": "application/pdf",
            "metadata": {},
        },
    )
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "mime_unsupported"


def test_parse_missing_file_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_data_dir(monkeypatch, tmp_path)
    (tmp_path / "raw").mkdir(exist_ok=True)
    client = TestClient(create_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_003",
            "file_path": str(tmp_path / "raw" / "missing.md"),
            "mime_type": "text/markdown",
            "metadata": {},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "file_not_found"


def test_parse_embedding_dim_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_data_dir(monkeypatch, tmp_path)
    file_path = _write(tmp_path, "doc.md", "一段正文足够长以独立成块。")
    client = TestClient(create_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_004",
            "file_path": file_path,
            "mime_type": "text/plain",
            "metadata": {},
        },
    )
    body = resp.json()
    dims = {len(v) for v in body["embeddings"]}
    assert len(dims) == 1


def test_parse_rejects_path_outside_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_data_dir(monkeypatch, tmp_path)
    (tmp_path / "raw").mkdir(exist_ok=True)
    safe = tmp_path / "raw" / "x.md"
    safe.write_text("safe", encoding="utf-8")

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    client = TestClient(create_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_safe",
            "file_path": str(safe),
            "mime_type": "text/plain",
            "metadata": {},
        },
    )
    assert resp.status_code == 200

    resp2 = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_outside",
            "file_path": str(outside),
            "mime_type": "text/plain",
            "metadata": {},
        },
    )
    assert resp2.status_code == 400
    assert resp2.json()["detail"]["error_code"] == "path_not_allowed"


def test_parse_rejects_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_data_dir(monkeypatch, tmp_path)
    client = TestClient(create_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": "doc_rel",
            "file_path": "x.md",
            "mime_type": "text/plain",
            "metadata": {},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "path_not_allowed"
