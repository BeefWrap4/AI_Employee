from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _upload(
    client: TestClient,
    *,
    title: str,
    content: str,
    metadata: dict,
    acl_tags: list[str],
    mime_type: str = "text/markdown",
):
    return client.post(
        "/api/v1/documents",
        files={"file": (f"{title}.md", content.encode("utf-8"), mime_type)},
        data={
            "title": title,
            "metadata_json": json.dumps(metadata),
            "acl_tags_json": json.dumps(acl_tags),
            "version": "v1",
            "mime_type": mime_type,
        },
    )


class _UnreachableWorker:
    """api_factory 注入用：模拟 worker 不可达。"""

    def __init__(self) -> None:
        from ai_employee.knowledge_api.worker_client import WorkerDispatchResult
        self._WorkerDispatchResult = WorkerDispatchResult

    def health(self) -> bool:
        return False

    def parse(self, doc_id, file_path, mime_type, metadata):
        return self._WorkerDispatchResult(
            dispatched=False,
            dispatch_status="worker_unreachable",
            error="disabled",
        )


def test_upload_to_publish_to_query_full_flow(api_factory) -> None:
    client = api_factory()

    created = _upload(
        client,
        title="光传输误码处理 SOP",
        content="传输链路误码升高时先核查端口误码、光功率和链路抖动。",
        metadata={"network_type": "transport", "domain": "transport"},
        acl_tags=["transport", "noc"],
    )
    assert created.status_code == 202
    doc_id = created.json()["doc_id"]
    assert created.json()["parse_status"] == "ready"
    assert created.json()["worker_dispatch"] == "accepted"

    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks")
    assert chunks.status_code == 200
    assert len(chunks.json()["chunks"]) >= 1

    published = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert published.status_code == 200
    assert published.json()["parse_status"] == "published"

    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_flow",
            "question": "传输链路误码升高时先核查端口误码、光功率和链路抖动。",
            "knowledge_scopes": ["transport", "noc"],
            "stream": False,
        },
    )
    assert answer.status_code == 200
    assert "光功率" in answer.json()["answer"]
    assert answer.json()["citations"][0]["doc_title"] == "光传输误码处理 SOP"


def test_publish_before_ready_returns_409(api_factory) -> None:
    # 用不可达 worker 让文档停在 uploaded
    client = api_factory(worker_client=_UnreachableWorker())

    created = _upload(
        client,
        title="SOP",
        content="正文内容。",
        metadata={"network_type": "5g"},
        acl_tags=["wireless"],
    )
    assert created.status_code == 202
    doc_id = created.json()["doc_id"]
    assert created.json()["parse_status"] == "uploaded"

    resp = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "not_ready"
