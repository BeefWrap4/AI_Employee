from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from ai_employee.ingestion_worker.app import create_app as create_worker_app
from ai_employee.knowledge_api.app import create_app as create_api_app
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# R24-A.4: ensure every test sees the legacy internal-token env var so the
# ``require_oidc_or_internal`` fallback path accepts requests built by
# the existing test helpers without per-test plumbing.
# --------------------------------------------------------------------------- #


INTERNAL_TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the cross-service internal token + JWT secret used by the auth dep.

    Also clears any OIDC env that a previous test might have set, so
    the OIDC branch is off by default and the legacy internal-token
    path is exercised.  Tests that exercise OIDC explicitly re-enable
    the OIDC env vars via ``monkeypatch.setenv``.
    """
    monkeypatch.setenv("INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("JWT_SECRET", "test-secret-please-rotate-super-long-key-32b")
    monkeypatch.delenv("JWT_AUTH_STRICT", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


@pytest.fixture(autouse=True)
def _patch_test_client_default_headers() -> Any:
    """Wrap :class:`fastapi.testclient.TestClient` so every constructed
    client automatically sends ``X-Internal-Token`` by default.

    The auth dep wired into the production write endpoints requires
    OIDC / JWT / internal-token credentials.  Existing tests build
    ``TestClient(app)`` without headers — after this patch, those
    calls behave as if they had ``headers={"X-Internal-Token":
    "test-token"}`` so the legacy path authenticates without per-test
    plumbing.

    To opt out (e.g. when asserting that an unauthenticated request is
    rejected), pass ``_internal_token=False`` to TestClient.  This is
    a sentinel we recognise; the real FastAPI TestClient ignores
    unknown kwargs (and in any case we strip the sentinel before
    forwarding kwargs to the original ``__init__``).
    """
    from fastapi import testclient as _tc

    original_init = _tc.TestClient.__init__

    def patched_init(self, *args: Any, **kwargs: Any) -> None:
        inject = kwargs.pop("_internal_token", True)
        headers = dict(kwargs.pop("headers", None) or {})
        if inject and "X-Internal-Token" not in headers:
            headers["X-Internal-Token"] = INTERNAL_TOKEN
        kwargs["headers"] = headers
        original_init(self, *args, **kwargs)

    _tc.TestClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        yield
    finally:
        _tc.TestClient.__init__ = original_init  # type: ignore[assignment]


@pytest.fixture
def knowledge_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "knowledge.sqlite3"
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KNOWLEDGE_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("KNOWLEDGE_API_INTERNAL_TOKEN", "test-token")
    # R24-A.4: the production write dep falls back to a service-specific
    # internal token (KNOWLEDGE_API_INTERNAL_TOKEN); the TestClient
    # autouse fixture below auto-injects that token so existing tests
    # keep working without explicit X-Internal-Token headers.
    monkeypatch.setenv("INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("INGESTION_WORKER_URL", "http://in-process")
    return data_dir


class InProcessWorkerClient(WorkerClient):
    """测试用：不走真实 HTTP，直接调用 worker app 的 TestClient。"""

    def __init__(self, worker_app=None) -> None:
        self._client = TestClient(worker_app or create_worker_app())
        self._reachable = True

    def set_reachable(self, reachable: bool) -> None:
        self._reachable = reachable

    def health(self) -> bool:
        return self._reachable

    def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
        if not self._reachable:
            return WorkerDispatchResult(
                dispatched=False,
                dispatch_status="worker_unreachable",
                error="in-process worker disabled",
            )
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
            from ai_employee.common_schemas.knowledge import ParseResponse

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


@pytest.fixture
def api_factory(knowledge_workspace: Path):
    def _factory(worker_client=None) -> TestClient:
        store = SQLiteStore(
            db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
            data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
        )
        store.init_schema()
        wc = worker_client or InProcessWorkerClient()
        app = create_api_app(store=store, worker_client=wc)
        return TestClient(app)

    return _factory


@pytest.fixture
def client(api_factory) -> TestClient:
    return api_factory()


def _upload(
    client: TestClient,
    *,
    title: str,
    content: str,
    metadata: dict,
    acl_tags: list[str],
    mime_type: str = "text/markdown",
    version: str = "v1",
):
    return client.post(
        "/api/v1/documents",
        files={"file": (f"{title}.md", content.encode("utf-8"), mime_type)},
        data={
            "title": title,
            "metadata_json": json.dumps(metadata),
            "acl_tags_json": json.dumps(acl_tags),
            "version": version,
            "mime_type": mime_type,
        },
    )


def _upload_and_publish(
    client: TestClient,
    *,
    title: str,
    content: str,
    metadata: dict,
    acl_tags: list[str],
) -> str:
    created = _upload(client, title=title, content=content, metadata=metadata, acl_tags=acl_tags)
    assert created.status_code == 202, created.text
    doc_id = created.json()["doc_id"]
    assert created.json()["parse_status"] == "ready"
    published = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert published.status_code == 200
    return doc_id
