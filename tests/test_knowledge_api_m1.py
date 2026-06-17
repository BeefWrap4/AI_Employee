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


def test_health_reports_sqlite_storage(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "knowledge-api"
    assert body["storage"] == "sqlite"
    assert body["ingestion_worker_reachable"] is True


def test_upload_parses_to_ready_and_publish_then_query_and_feedback(client: TestClient) -> None:
    created = _upload(
        client,
        title="5G RRC 建立失败处理 SOP",
        content="RRC 建立失败时先检查告警、KPI、传输链路和近期参数变更。",
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless", "noc"],
    )
    assert created.status_code == 202
    body = created.json()
    assert body["parse_status"] == "ready"
    assert body["chunk_count"] == 1
    assert body["worker_dispatch"] == "accepted"
    assert body["trace_id"].startswith("trace_")
    doc_id = body["doc_id"]

    fetched = client.get(f"/api/v1/documents/{doc_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "5G RRC 建立失败处理 SOP"
    assert fetched.json()["mime_type"] == "text/markdown"

    published = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert published.status_code == 200
    assert published.json()["parse_status"] == "published"

    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_001",
            "question": "5G 小区 RRC 建立失败率升高先查什么？",
            "knowledge_scopes": ["wireless", "5g"],
            "stream": False,
        },
    )
    # 检索命中（问题与 chunk 共享 RRC/建立失败等文本）
    assert answer.status_code == 200, answer.text
    abody = answer.json()
    assert "RRC 建立失败" in abody["answer"]
    assert abody["confidence"] >= 0
    assert abody["citations"][0]["doc_title"] == "5G RRC 建立失败处理 SOP"

    feedback = client.post(
        "/api/v1/feedback",
        json={"trace_id": abody["trace_id"], "feedback_type": "useful", "comment": "引用清楚"},
    )
    assert feedback.status_code == 201
    assert feedback.json()["feedback_id"].startswith("fb_")


def test_query_selects_best_matching_published_document(client: TestClient) -> None:
    _upload_and_publish(
        client,
        title="5G RRC 建立失败处理 SOP",
        content="RRC 建立失败时先检查无线侧告警和接入 KPI。",
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless", "noc"],
    )
    transport_doc_id = _upload_and_publish(
        client,
        title="传输链路误码处理 SOP",
        content="传输链路误码升高时先核查端口误码、光功率、链路抖动和割接记录。",
        metadata={"network_type": "transport", "domain": "transport"},
        acl_tags=["transport", "noc"],
    )
    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_transport",
            "question": "传输链路误码升高时先核查端口误码、光功率、链路抖动和割接记录。",
            "knowledge_scopes": ["transport", "noc"],
            "stream": False,
        },
    )
    assert answer.status_code == 200
    assert answer.json()["citations"][0]["doc_title"] == "传输链路误码处理 SOP"
    assert answer.json()["citations"][0]["chunk_id"].startswith(f"chunk_{transport_doc_id}")


def test_query_filters_documents_outside_knowledge_scope(client: TestClient) -> None:
    wireless_doc_id = _upload_and_publish(
        client,
        title="5G RRC 建立失败处理 SOP",
        content="RRC 建立失败时先检查无线侧告警和接入 KPI。",
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless"],
    )
    _upload_and_publish(
        client,
        title="传输链路误码处理 SOP",
        content="传输链路误码升高时先核查端口误码、光功率、链路抖动和割接记录。",
        metadata={"network_type": "transport", "domain": "transport"},
        acl_tags=["transport"],
    )
    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_wireless_only",
            "question": "RRC 建立失败时先检查无线侧告警和接入 KPI。",
            "knowledge_scopes": ["wireless"],
            "stream": False,
        },
    )
    assert answer.status_code == 200
    assert answer.json()["citations"][0]["chunk_id"].startswith(f"chunk_{wireless_doc_id}")


def test_paragraph_chunks_listed_and_best_chunk_cited(client: TestClient) -> None:
    doc_id = _upload_and_publish(
        client,
        title="5G 接入与传输联合排障 SOP",
        content=(
            "RRC 建立失败时先检查无线侧告警和接入 KPI。\n\n"
            "传输链路误码升高时先核查端口误码、光功率和链路抖动。"
        ),
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless", "transport", "noc"],
    )
    fetched = client.get(f"/api/v1/documents/{doc_id}")
    assert fetched.json()["chunk_count"] == 2

    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks")
    assert chunks.status_code == 200
    contents = [c["content"] for c in chunks.json()["chunks"]]
    assert any("光功率" in c for c in contents)

    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_chunk",
            "question": "传输链路误码升高时先核查端口误码、光功率和链路抖动。",
            "knowledge_scopes": ["transport", "noc"],
            "stream": False,
        },
    )
    assert answer.status_code == 200, answer.text
    assert "光功率" in answer.json()["answer"]


def test_chunks_endpoint_404_for_unknown_doc(client: TestClient) -> None:
    resp = client.get("/api/v1/documents/doc_unknown/chunks")
    assert resp.status_code == 404
