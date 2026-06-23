from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# ruff: noqa: E402 — the warning filter must be installed before importing
# starlette.testclient, and the testclient-import itself comes last so the
# deprecation warning from httpx is suppressed.
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

import httpx
from ai_employee.common_schemas.knowledge import ParseResponse
from ai_employee.ingestion_worker.app import create_app as create_worker_app
from ai_employee.knowledge_api.app import create_app as create_api_app
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# M1 smoke flow surface
# --------------------------------------------------------------------------- #


@runtime_checkable
class Smoke(Protocol):
    """A backend for the 5-step M1 knowledge smoke flow.

    Two implementations exist:
    * :class:`InProcessSmoke` — the original TestClient-driven flow that
      spins up knowledge-api + ingestion-worker inside one Python process.
    * :class:`HttpSmoke` — drives a running api-gateway (R32-A) over HTTP,
      so the same script can validate a deployed cluster.
    """

    def run(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# In-process smoke (default; preserved behaviour for CI)
# --------------------------------------------------------------------------- #


class InProcessWorkerClient(WorkerClient):
    def __init__(self) -> None:
        self._client = TestClient(create_worker_app())

    def health(self) -> bool:
        return True

    def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
        response = self._client.post(
            "/internal/parse",
            json={
                "doc_id": doc_id,
                "file_path": file_path,
                "mime_type": mime_type,
                "metadata": metadata,
            },
        )
        if response.status_code == 200:
            return WorkerDispatchResult(
                dispatched=True,
                dispatch_status="accepted",
                response=ParseResponse(**response.json()),
            )
        return WorkerDispatchResult(
            dispatched=False,
            dispatch_status="worker_error",
            error=f"worker returned {response.status_code}: {response.text}",
        )


class InProcessSmoke:
    """Run the M1 flow entirely in-process (TestClient + SQLite).

    The original default behaviour, preserved verbatim so the local CI
    smoke (pytest ``test_m1_smoke_script.py``) and any operator running
    the script on a workstation without a cluster keep working.

    Env vars touched by the flow are snapshotted on entry and restored
    on exit, so importing this module from a unit test (or running it
    inside a larger pytest session) does not leak stub embedding
    dimensions / worker URLs into sibling tests.
    """

    # Env vars the in-process flow needs to set inside :meth:`_client`.
    _SCRATCH_ENV = (
        "KNOWLEDGE_DATA_DIR",
        "KNOWLEDGE_SQLITE_PATH",
        "KNOWLEDGE_API_INTERNAL_TOKEN",
        "INGESTION_WORKER_URL",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_DIM",
    )

    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)

    def _client(self) -> TestClient:
        os.environ["KNOWLEDGE_DATA_DIR"] = str(self._data_dir)
        os.environ["KNOWLEDGE_SQLITE_PATH"] = str(self._data_dir / "knowledge.sqlite3")
        os.environ["KNOWLEDGE_API_INTERNAL_TOKEN"] = "local-smoke-token"
        os.environ["INGESTION_WORKER_URL"] = "http://in-process"
        os.environ["EMBEDDING_PROVIDER"] = "stub"
        os.environ["EMBEDDING_DIM"] = "8"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        store = SQLiteStore(
            db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
            data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
        )
        store.init_schema()
        return TestClient(
            create_api_app(store=store, worker_client=InProcessWorkerClient()),
            headers={"X-Internal-Token": os.environ["KNOWLEDGE_API_INTERNAL_TOKEN"]},
        )

    def run(self) -> dict[str, Any]:
        # Snapshot the env keys we touch so we can restore on the way out.
        saved = {k: os.environ.get(k) for k in self._SCRATCH_ENV}
        try:
            return self._run()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _run(self) -> dict[str, Any]:
        client = self._client()
        # R24-A.4: the production write endpoints require authentication.
        # Local smoke scripts use the service-specific X-Internal-Token
        # fallback set on the TestClient above; production callers should
        # use OIDC or a JWT.
        content = (
            "# 5G RRC 建立失败处理 SOP\n\n"
            "RRC 建立失败时先检查无线侧告警和接入 KPI。\n\n"
            "如果伴随传输链路误码，继续核查端口误码、光功率和链路抖动。"
        )

        created = client.post(
            "/api/v1/documents",
            files={"file": ("rrc-sop.md", content.encode("utf-8"), "text/markdown")},
            data={
                "title": "5G RRC 建立失败处理 SOP",
                "metadata_json": json.dumps({"network_type": "5g", "domain": "wireless"}),
                "acl_tags_json": json.dumps(["wireless", "noc", "transport"]),
                "version": "v1",
                "mime_type": "text/markdown",
            },
        )
        created.raise_for_status()
        doc_id = created.json()["doc_id"]

        published = client.post(f"/api/v1/documents/{doc_id}/publish")
        published.raise_for_status()

        query = client.post(
            "/api/v1/chat/query",
            json={
                "session_id": "smoke",
                "question": "RRC 建立失败并伴随链路误码时先查什么？",
                "knowledge_scopes": ["wireless", "transport", "noc"],
                "stream": False,
            },
        )
        query.raise_for_status()
        query_body = query.json()

        feedback = client.post(
            "/api/v1/feedback",
            json={
                "trace_id": query_body["trace_id"],
                "feedback_type": "useful",
                "comment": "local smoke passed",
            },
        )
        feedback.raise_for_status()

        qa_logs = client.get("/api/v1/qa-logs")
        qa_logs.raise_for_status()
        feedbacks = client.get("/api/v1/feedbacks")
        feedbacks.raise_for_status()

        return {
            "document": {
                "doc_id": doc_id,
                "parse_status": published.json()["parse_status"],
                "chunk_count": published.json()["chunk_count"],
            },
            "query": {
                "trace_id": query_body["trace_id"],
                "confidence": query_body["confidence"],
                "citation_count": len(query_body["citations"]),
                "first_citation": query_body["citations"][0],
            },
            "feedback": feedback.json(),
            "audit": {
                "qa_log_total": qa_logs.json()["total"],
                "feedback_total": feedbacks.json()["total"],
            },
        }


# --------------------------------------------------------------------------- #
# HTTP smoke (R35-C: drive a running cluster via api-gateway)
# --------------------------------------------------------------------------- #


class HttpSmoke:
    """Drive the M1 flow against a running api-gateway.

    The 5-step flow maps 1:1 to the api-gateway's ``/api/knowledge/*``
    routes (R32-A); see ``services/api-gateway/app.py`` ``ROUTE_TABLE``
    and ``services/knowledge-api/app.py`` for the underlying handlers.

    Usage::

        python scripts/m1_smoke.py --cluster http://localhost:8070 --json

    The base URL is the api-gateway front door (port 8070); the
    gateway then forwards to knowledge-api:8010.  ``--cluster`` is
    optional; when omitted the in-process flow runs instead (preserving
    the existing CI behaviour).
    """

    def __init__(self, *, base_url: str, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    # ----- low-level helpers ----- #

    def _url(self, *parts: str) -> str:
        joined = "/".join(p.strip("/") for p in parts)
        return f"{self._base}/{joined}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
    ) -> httpx.Response:
        return httpx.request(
            method,
            self._url(path),
            json=json,
            data=data,
            files=files,
            timeout=self._timeout,
        )

    # ----- flow ----- #

    def run(self) -> dict[str, Any]:
        content = (
            "# 5G RRC 建立失败处理 SOP\n\n"
            "RRC 建立失败时先检查无线侧告警和接入 KPI。\n\n"
            "如果伴随传输链路误码，继续核查端口误码、光功率和链路抖动。"
        ).encode()

        # 1. upload -> create_document
        created = self._request(
            "POST",
            "api/knowledge/api/v1/documents",
            data={
                "title": "5G RRC 建立失败处理 SOP",
                "metadata_json": json.dumps({"network_type": "5g", "domain": "wireless"}),
                "acl_tags_json": json.dumps(["wireless", "noc", "transport"]),
                "version": "v1",
                "mime_type": "text/markdown",
            },
            files={"file": ("rrc-sop.md", content, "text/markdown")},
        )
        created.raise_for_status()
        doc_id = created.json()["doc_id"]

        # 2. publish
        published = self._request("POST", f"api/knowledge/api/v1/documents/{doc_id}/publish")
        published.raise_for_status()

        # 3. query
        query = self._request(
            "POST",
            "api/knowledge/api/v1/chat/query",
            json={
                "session_id": "smoke",
                "question": "RRC 建立失败并伴随链路误码时先查什么？",
                "knowledge_scopes": ["wireless", "transport", "noc"],
                "stream": False,
            },
        )
        query.raise_for_status()
        query_body = query.json()

        # 4. feedback
        feedback = self._request(
            "POST",
            "api/knowledge/api/v1/feedback",
            json={
                "trace_id": query_body["trace_id"],
                "feedback_type": "useful",
                "comment": "cluster smoke passed",
            },
        )
        feedback.raise_for_status()

        # 5. audit
        qa_logs = self._request("GET", "api/knowledge/api/v1/qa-logs")
        qa_logs.raise_for_status()
        feedbacks = self._request("GET", "api/knowledge/api/v1/feedbacks")
        feedbacks.raise_for_status()

        return {
            "document": {
                "doc_id": doc_id,
                "parse_status": published.json()["parse_status"],
                "chunk_count": published.json()["chunk_count"],
            },
            "query": {
                "trace_id": query_body["trace_id"],
                "confidence": query_body["confidence"],
                "citation_count": len(query_body["citations"]),
                "first_citation": query_body["citations"][0],
            },
            "feedback": feedback.json(),
            "audit": {
                "qa_log_total": qa_logs.json()["total"],
                "feedback_total": feedbacks.json()["total"],
            },
        }


# --------------------------------------------------------------------------- #
# Factory + CLI
# --------------------------------------------------------------------------- #


def build_smoke(*, cluster: str | None, data_dir: Path | None) -> Smoke:
    """Pick the right :class:`Smoke` implementation for the invocation.

    ``cluster`` is the api-gateway base URL (e.g. ``http://localhost:8070``).
    ``data_dir`` is the local SQLite scratch directory used only by the
    in-process flow.
    """
    if cluster:
        return HttpSmoke(base_url=cluster)
    target = (
        data_dir if data_dir is not None else Path(tempfile.mkdtemp(prefix="ai-employee-smoke-"))
    )
    return InProcessSmoke(data_dir=target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local M1 knowledge smoke flow.")
    parser.add_argument("--data-dir", default=None, help="data dir for SQLite and files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--cluster",
        default=None,
        help=(
            "base URL of a running api-gateway cluster (R35-C). "
            "When set, the smoke flow is driven over HTTP against the cluster "
            "instead of running knowledge-api + ingestion-worker in-process."
        ),
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else None
    smoke = build_smoke(cluster=args.cluster, data_dir=data_dir)
    summary = smoke.run()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("M1 smoke flow passed")
        print(
            f"  document: {summary['document']['doc_id']} ({summary['document']['chunk_count']} chunks)"
        )
        print(f"  query trace: {summary['query']['trace_id']}")
        print(f"  citations: {summary['query']['citation_count']}")
        print(f"  qa logs: {summary['audit']['qa_log_total']}")
        print(f"  feedbacks: {summary['audit']['feedback_total']}")
        if args.cluster:
            print(f"  cluster: {args.cluster}")
        else:
            print(f"  data dir: {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
