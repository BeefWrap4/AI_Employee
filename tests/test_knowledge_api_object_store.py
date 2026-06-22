"""Regression: knowledge-api uploads also write through to the object store (R22).

The on-disk ``raw/{doc_id}.{ext}`` path is still produced so the
ingestion worker can read it (the parser is path-based).  In addition,
the same bytes are stored under ``documents/{uuid}.{ext}`` in the
configured :class:`ObjectStore` (LocalFs by default in dev/test).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "knowledge.sqlite3"
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KNOWLEDGE_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("KNOWLEDGE_API_INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("INGESTION_WORKER_URL", "http://in-process")
    # LocalFs object store under a clean directory so we can assert
    # the write-through.
    monkeypatch.delenv("OBJECT_STORE_URL", raising=False)
    obj_root = tmp_path / "objects"
    monkeypatch.setenv("OBJECT_STORE_LOCAL_ROOT", str(obj_root))
    return data_dir, obj_root


def test_upload_writes_to_object_store(workspace) -> None:
    from ai_employee.common_schemas.knowledge import ParseResponse
    from ai_employee.ingestion_worker.app import create_app as create_worker_app
    from ai_employee.knowledge_api.app import create_app as create_api_app
    from ai_employee.knowledge_api.store import SQLiteStore
    from ai_employee.knowledge_api.worker_client import (
        WorkerClient,
        WorkerDispatchResult,
    )
    from fastapi.testclient import TestClient

    data_dir, obj_root = workspace
    store = SQLiteStore(
        db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
        data_dir=str(data_dir),
    )
    store.init_schema()

    class InProcessWorker(WorkerClient):
        def __init__(self) -> None:
            self._client = TestClient(create_worker_app())
            self._reachable = True

        def health(self) -> bool:
            return self._reachable

        def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
            resp = self._client.post(
                "/internal/parse",
                json={
                    "doc_id": doc_id,
                    "file_path": file_path,
                    "mime_type": mime_type,
                    "metadata": metadata,
                },
            )
            if resp.status_code == 200:
                return WorkerDispatchResult(
                    dispatched=True,
                    dispatch_status="accepted",
                    response=ParseResponse(**resp.json()),
                )
            return WorkerDispatchResult(
                dispatched=False,
                dispatch_status="worker_error",
                error=f"worker returned {resp.status_code}: {resp.text}",
            )

    app = create_api_app(store=store, worker_client=InProcessWorker())
    client = TestClient(app)

    resp = client.post(
        "/api/v1/documents",
        data={
            "title": "whitepaper",
            "metadata_json": "{}",
            "acl_tags_json": "[]",
            "version": "v1",
            "mime_type": "text/plain",
        },
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 202, resp.text
    doc_id = resp.json()["doc_id"]

    # The ingestion worker probably parsed synchronously; check the
    # local disk AND the object store.
    doc = client.get(f"/api/v1/documents/{doc_id}").json()
    assert doc["metadata"].get("object_key") is not None
    obj_key = doc["metadata"]["object_key"]
    # The default LocalFs backend writes under OBJECT_STORE_LOCAL_ROOT.
    target = obj_root / obj_key
    assert target.is_file(), f"object store missing {target}"
    assert target.read_bytes() == b"hello world"
