from fastapi.testclient import TestClient

from ai_employee.knowledge_api.app import create_app


def _create_and_publish_document(
    client: TestClient,
    *,
    title: str,
    content: str,
    metadata: dict[str, str],
    acl_tags: list[str],
) -> str:
    created = client.post(
        "/api/v1/documents",
        json={
            "title": title,
            "content": content,
            "metadata": metadata,
            "acl_tags": acl_tags,
        },
    )
    assert created.status_code == 201
    doc_id = created.json()["doc_id"]

    published = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert published.status_code == 200

    return doc_id


def test_knowledge_api_document_query_and_feedback_flow() -> None:
    client = TestClient(create_app())

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "service": "knowledge-api",
        "status": "ok",
        "version": "0.1.0",
    }

    created = client.post(
        "/api/v1/documents",
        json={
            "title": "5G RRC 建立失败处理 SOP",
            "content": "RRC 建立失败时先检查告警、KPI、传输链路和近期参数变更。",
            "metadata": {"network_type": "5g", "domain": "wireless"},
            "acl_tags": ["wireless", "noc"],
        },
    )
    assert created.status_code == 201
    created_body = created.json()
    assert created_body["parse_status"] == "uploaded"
    assert created_body["chunk_count"] == 1
    assert created_body["trace_id"].startswith("trace_")

    doc_id = created_body["doc_id"]
    document = client.get(f"/api/v1/documents/{doc_id}")
    assert document.status_code == 200
    assert document.json()["title"] == "5G RRC 建立失败处理 SOP"
    assert document.json()["chunk_count"] == 1

    published = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert published.status_code == 200
    assert published.json()["parse_status"] == "published"
    assert published.json()["chunk_count"] == 1

    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_001",
            "question": "5G 小区 RRC 建立失败率升高先查什么？",
            "knowledge_scopes": ["wireless", "5g"],
            "stream": False,
        },
    )
    assert answer.status_code == 200
    answer_body = answer.json()
    assert "RRC 建立失败" in answer_body["answer"]
    assert answer_body["confidence"] > 0
    assert answer_body["citations"] == [
        {
            "chunk_id": f"chunk_{doc_id}_001",
            "doc_title": "5G RRC 建立失败处理 SOP",
            "page_no": 1,
            "section_path": "root",
        }
    ]

    feedback = client.post(
        "/api/v1/feedback",
        json={
            "trace_id": answer_body["trace_id"],
            "feedback_type": "useful",
            "comment": "引用清楚",
        },
    )
    assert feedback.status_code == 201
    assert feedback.json()["feedback_id"].startswith("fb_")


def test_query_selects_best_matching_published_document() -> None:
    client = TestClient(create_app())
    _create_and_publish_document(
        client,
        title="5G RRC 建立失败处理 SOP",
        content="RRC 建立失败时先检查无线侧告警和接入 KPI。",
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless", "noc"],
    )
    transport_doc_id = _create_and_publish_document(
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
            "question": "传输链路误码升高先查什么？",
            "knowledge_scopes": ["transport", "noc"],
            "stream": False,
        },
    )

    assert answer.status_code == 200
    assert answer.json()["citations"][0]["chunk_id"] == f"chunk_{transport_doc_id}_001"
    assert answer.json()["citations"][0]["doc_title"] == "传输链路误码处理 SOP"


def test_query_filters_documents_outside_knowledge_scope() -> None:
    client = TestClient(create_app())
    wireless_doc_id = _create_and_publish_document(
        client,
        title="5G RRC 建立失败处理 SOP",
        content="RRC 建立失败时先检查无线侧告警和接入 KPI。",
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless"],
    )
    _create_and_publish_document(
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
            "question": "传输链路误码升高先查什么？",
            "knowledge_scopes": ["wireless"],
            "stream": False,
        },
    )

    assert answer.status_code == 200
    assert answer.json()["citations"][0]["chunk_id"] == f"chunk_{wireless_doc_id}_001"
    assert answer.json()["citations"][0]["doc_title"] == "5G RRC 建立失败处理 SOP"


def test_document_content_is_split_into_paragraph_chunks_and_query_cites_best_chunk() -> None:
    client = TestClient(create_app())
    doc_id = _create_and_publish_document(
        client,
        title="5G 接入与传输联合排障 SOP",
        content=(
            "RRC 建立失败时先检查无线侧告警和接入 KPI。\n\n"
            "传输链路误码升高时先核查端口误码、光功率和链路抖动。"
        ),
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless", "transport", "noc"],
    )

    document = client.get(f"/api/v1/documents/{doc_id}")
    assert document.status_code == 200
    assert document.json()["chunk_count"] == 2

    answer = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s_chunk",
            "question": "链路误码升高要查什么？",
            "knowledge_scopes": ["transport", "noc"],
            "stream": False,
        },
    )

    assert answer.status_code == 200
    assert "光功率" in answer.json()["answer"]
    assert answer.json()["citations"][0]["chunk_id"] == f"chunk_{doc_id}_002"


def test_document_chunks_can_be_listed_for_source_location() -> None:
    client = TestClient(create_app())
    doc_id = _create_and_publish_document(
        client,
        title="5G 接入排障 SOP",
        content=(
            "第一步检查 RRC 建立失败相关告警。\n\n"
            "第二步检查接入 KPI 和近期参数变更。"
        ),
        metadata={"network_type": "5g", "domain": "wireless"},
        acl_tags=["wireless", "noc"],
    )

    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks")

    assert chunks.status_code == 200
    assert chunks.json() == {
        "doc_id": doc_id,
        "chunks": [
            {
                "chunk_id": f"chunk_{doc_id}_001",
                "content": "第一步检查 RRC 建立失败相关告警。",
                "page_no": 1,
                "section_path": "root",
            },
            {
                "chunk_id": f"chunk_{doc_id}_002",
                "content": "第二步检查接入 KPI 和近期参数变更。",
                "page_no": 1,
                "section_path": "root",
            },
        ],
    }
