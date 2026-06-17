import json

from fastapi.testclient import TestClient


def _upload_and_publish(client: TestClient, *, title: str, content: str,
                         metadata: dict, acl_tags: list[str]) -> str:
    r = client.post(
        "/api/v1/documents",
        files={"file": (f"{title}.md", content.encode("utf-8"), "text/markdown")},
        data={
            "title": title,
            "metadata_json": json.dumps(metadata),
            "acl_tags_json": json.dumps(acl_tags),
            "version": "v1",
            "mime_type": "text/markdown",
        },
    )
    assert r.status_code == 202
    doc_id = r.json()["doc_id"]
    assert client.post(f"/api/v1/documents/{doc_id}/publish").status_code == 200
    return doc_id


def _query(client: TestClient, *, session: str, question: str, scopes: list[str]) -> dict:
    r = client.post(
        "/api/v1/chat/query",
        json={"session_id": session, "question": question, "knowledge_scopes": scopes, "stream": False},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_list_documents_published(client: TestClient) -> None:
    d1 = _upload_and_publish(
        client, title="5G 接入排障 SOP",
        content="RRC 建立失败先检查告警和接入 KPI。",
        metadata={"network_type": "5g"}, acl_tags=["wireless"],
    )
    d2 = _upload_and_publish(
        client, title="传输链路 SOP",
        content="光功率下降核查端口误码。",
        metadata={"network_type": "transport"}, acl_tags=["transport"],
    )
    r = client.get("/api/v1/documents?status=published&page=1&page_size=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {i["doc_id"] for i in body["items"]} == {d1, d2}
    for it in body["items"]:
        assert it["parse_status"] == "published"
        assert it["chunk_count"] >= 1


def test_list_documents_pagination(client: TestClient) -> None:
    for i in range(5):
        _upload_and_publish(
            client, title=f"D{i}", content=f"c{i}",
            metadata={}, acl_tags=[],
        )
    r = client.get("/api/v1/documents?page=1&page_size=2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 1
    r2 = client.get("/api/v1/documents?page=3&page_size=2")
    assert len(r2.json()["items"]) == 1


def test_qa_log_round_trip(client: TestClient) -> None:
    _upload_and_publish(
        client, title="5G SOP",
        content="RRC 建立失败先查告警。",
        metadata={"network_type": "5g"}, acl_tags=["wireless"],
    )
    answer = _query(
        client, session="sess_audit_1",
        question="RRC 建立失败先查告警。",
        scopes=["wireless"],
    )
    trace_id = answer["trace_id"]

    r = client.get(f"/api/v1/qa-logs/{trace_id}")
    assert r.status_code == 200
    log = r.json()
    assert log["trace_id"] == trace_id
    assert log["question"] == "RRC 建立失败先查告警。"
    assert "RRC" in log["answer"]
    assert log["retrieved_chunks"]  # list 非空
    assert log["retrieved_chunks"][0]["chunk_id"].startswith("chunk_")


def test_get_qa_log_missing_404(client: TestClient) -> None:
    r = client.get("/api/v1/qa-logs/does_not_exist")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "qa_log_not_found"


def test_list_qa_logs_filter_session(client: TestClient) -> None:
    _upload_and_publish(
        client, title="5G SOP",
        content="RRC 建立失败先查告警。",
        metadata={"network_type": "5g"}, acl_tags=["wireless"],
    )
    _query(client, session="sA1", question="RRC 建立失败先查告警。", scopes=["wireless"])
    _query(client, session="sB1", question="RRC 建立失败先查告警。", scopes=["wireless"])
    _query(client, session="sA2", question="RRC 建立失败先查告警。", scopes=["wireless"])
    # sA1 + sA2 属于"以 sA 开头"，但精确匹配 session_id=sA1 -> 1；sA2 -> 1
    r = client.get("/api/v1/qa-logs?session_id=sA1")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    # 查全部，前缀匹配的没有；sA1/sA2 各自 1
    r2 = client.get("/api/v1/qa-logs")
    assert r2.json()["total"] == 3
    sessions = {i["session_id"] for i in r2.json()["items"]}
    assert sessions == {"sA1", "sA2", "sB1"}


def test_feedback_list_filter_trace(client: TestClient) -> None:
    _upload_and_publish(
        client, title="5G SOP",
        content="RRC 建立失败先查告警。",
        metadata={"network_type": "5g"}, acl_tags=["wireless"],
    )
    answer = _query(
        client, session="sFB", question="RRC 建立失败先查告警。",
        scopes=["wireless"],
    )
    trace_id = answer["trace_id"]
    # 提交 feedback
    fr = client.post("/api/v1/feedback", json={"trace_id": trace_id, "feedback_type": "useful", "comment": "好"})
    assert fr.status_code == 201

    r = client.get(f"/api/v1/feedbacks?trace_id={trace_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["feedback_type"] == "useful"
    assert body["items"][0]["comment"] == "好"
    assert body["items"][0]["trace_id"] == trace_id


def test_list_qa_logs_since_filter(client: TestClient) -> None:
    _upload_and_publish(
        client, title="5G SOP",
        content="RRC 建立失败先查告警。",
        metadata={"network_type": "5g"}, acl_tags=["wireless"],
    )
    _query(client, session="sZ", question="RRC 建立失败先查告警。", scopes=["wireless"])
    # 设一个很早的 since，应返回 0
    r = client.get("/api/v1/qa-logs?since=2000-01-01T00:00:00%2B00:00&until=2000-01-02T00:00:00%2B00:00")
    assert r.status_code == 200
    assert r.json()["total"] == 0
    # 一个晚的 since，应返回 1
    r2 = client.get("/api/v1/qa-logs?since=2000-01-01T00:00:00%2B00:00")
    assert r2.json()["total"] >= 1


def test_qa_log_records_knowledge_scopes(client: TestClient) -> None:
    _upload_and_publish(
        client,
        title="Public SOP",
        content="public rrc troubleshooting evidence",
        metadata={"network_type": "5g"},
        acl_tags=[],
    )
    answer = _query(
        client,
        session="sess_scope_audit",
        question="public rrc troubleshooting evidence",
        scopes=["wireless", "5g"],
    )

    r = client.get(f"/api/v1/qa-logs/{answer['trace_id']}")
    assert r.status_code == 200
    assert r.json()["knowledge_scopes"] == ["wireless", "5g"]
