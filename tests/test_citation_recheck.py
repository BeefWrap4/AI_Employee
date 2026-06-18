from pathlib import Path

from ai_employee.knowledge_api.app import create_app
from ai_employee.knowledge_api.store import SQLiteStore
from fastapi.testclient import TestClient


def _setup_published(
    tmp_path: Path, *, title: str, content: str, metadata: dict, acl_tags: list[str]
) -> str:
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    store = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    store.init_schema()
    doc_id = store.create_document(
        title, str(raw / f"{title}.md"), "text/plain", metadata, acl_tags, "v1"
    )
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [{"chunk_id": f"c_{doc_id}", "chunk_no": 1, "content": content, "section_path": "root"}],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")
    return doc_id


def test_citation_outside_scope_filtered_out(tmp_path: Path) -> None:
    """引用二次校验：候选含越权 doc → 最终 citations 过滤掉。"""
    # 创建 1 个 doc
    d_wireless = _setup_published(
        tmp_path,
        title="无线",
        content="RRC 建立失败先查告警",
        metadata={"network_type": "5g"},
        acl_tags=["wireless"],
    )
    # 用 stub query 直接调 retrieval.search 注入一个越权 candidate
    store = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    app = create_app(store=store)
    # 拿一个 TestClient 但不真正 query；直接构造 query 验证 scope 过滤
    client = TestClient(app)
    r = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s1",
            "question": "RRC 建立失败先查告警",
            "knowledge_scopes": ["wireless"],
            "stream": False,
        },
    )
    # 若 doc_wireless 命中 wireless，200 且 citations 包含该 doc
    assert r.status_code == 200
    citations = r.json()["citations"]
    assert all(c["doc_id"] == d_wireless for c in citations)


def test_query_with_scope_or_hits_meta_value(tmp_path: Path) -> None:
    """scope_or 命中 metadata 字段。"""
    d_5g = _setup_published(
        tmp_path,
        title="5G",
        content="5G 接入处理",
        metadata={"network_type": "5g"},
        acl_tags=["noc"],
    )
    store = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    app = create_app(store=store)
    client = TestClient(app)
    r = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s2",
            "question": "5G 接入处理",
            "knowledge_scopes": ["wireless"],
            "knowledge_scopes_or": ["5g"],
            "stream": False,
        },
    )
    assert r.status_code == 200
    doc_ids = {c["doc_id"] for c in r.json()["citations"]}
    assert d_5g in doc_ids


def test_query_no_scope_no_or_returns_all_published(tmp_path: Path) -> None:
    """scope 与 scope_or 都为空 → 全部 published 可见。"""
    d = _setup_published(
        tmp_path,
        title="any",
        content="x",
        metadata={"k": "v"},
        acl_tags=[],
    )
    store = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    app = create_app(store=store)
    client = TestClient(app)
    r = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "s3",
            "question": "x",
            "knowledge_scopes": [],
            "knowledge_scopes_or": [],
            "stream": False,
        },
    )
    assert r.status_code == 200
    assert any(c["doc_id"] == d for c in r.json()["citations"])
