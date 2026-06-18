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


def test_openai_compat_missing_config_degrades_to_stub(api_factory) -> None:
    """worker 配置 openai_compat 但 base_url/api_key 缺失 → 自动降级 stub。"""
    import os

    from ai_employee.ingestion_worker.app import create_app as create_worker_app

    os.environ["EMBEDDING_PROVIDER"] = "openai_compat"
    os.environ["EMBEDDING_BASE_URL"] = ""
    os.environ["EMBEDDING_API_KEY"] = ""
    os.environ["EMBEDDING_MODEL"] = ""
    try:
        worker_app = create_worker_app()
        # 用降级后的 worker 构造 in-process client（在测试内联定义避免跨文件导入）
        from ai_employee.common_schemas.knowledge import ParseResponse
        from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult

        wc = TestClient(worker_app)

        class _InProcess(WorkerClient):
            def __init__(self) -> None:
                self.base_url = ""
                self.internal_token = ""
                self.timeout_s = 30.0

            def health(self) -> bool:
                return True

            def parse(self, doc_id, file_path, mime_type, metadata):
                resp = wc.post(
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
                    error=resp.text,
                )

        client = api_factory(worker_client=_InProcess())

        created = _upload(
            client,
            title="降级 SOP",
            content="降级时仍能完成解析与 embedding。",
            metadata={"network_type": "5g"},
            acl_tags=["wireless"],
        )
        assert created.status_code == 202
        assert created.json()["parse_status"] == "ready"
        assert created.json()["worker_dispatch"] == "accepted"

        # worker health 报告 stub（降级）
        wh = wc.get("/health")
        assert wh.json()["embedding_provider"] == "stub"
    finally:
        os.environ["EMBEDDING_PROVIDER"] = "stub"
        for k in ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL"):
            os.environ.pop(k, None)


def test_stub_provider_is_default_in_worker() -> None:
    """默认 provider 是 stub。"""
    from ai_employee.ingestion_worker.app import create_app as create_worker_app

    app = create_worker_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.json()["embedding_provider"] == "stub"
