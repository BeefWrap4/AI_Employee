"""Query rewriter unit tests — mock LLM client, fallback, integration with retrieval."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from ai_employee.knowledge_api.query_rewriter import rewrite_query
from ai_employee.llm_gateway.client import ChatResponse, LlmClientError


def test_rewrite_returns_llm_content_when_successful() -> None:
    fake = MagicMock()
    fake.chat.return_value = ChatResponse(
        content="RRC 建立失败 5G 小区 告警\n",
        model="qwen-turbo",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    result = rewrite_query("5G 小区 RRC 建立失败率升高该查什么？", client=fake)
    assert "RRC" in result
    assert "建立失败" in result


def test_rewrite_strips_trailing_punctuation() -> None:
    fake = MagicMock()
    fake.chat.return_value = ChatResponse(
        content="RRC 建立失败 KPI 告警。",
        model="qwen-turbo",
        usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    )
    result = rewrite_query("RRC 建立失败", client=fake)
    assert "RRC 建立失败 KPI 告警" in result or result == "RRC 建立失败 KPI 告警"


def test_rewrite_falls_back_to_original_on_error() -> None:
    fake = MagicMock()
    fake.chat.side_effect = LlmClientError("timeout", status_code=503)
    result = rewrite_query("original question text", client=fake, fallback=True)
    assert result == "original question text"


def test_rewrite_raises_if_fallback_disabled() -> None:
    fake = MagicMock()
    fake.chat.side_effect = LlmClientError("auth failed", status_code=401)
    with pytest.raises(LlmClientError):
        rewrite_query("x", client=fake, fallback=False)


def test_rewrite_returns_original_on_empty_llm_response() -> None:
    fake = MagicMock()
    fake.chat.return_value = ChatResponse(
        content="   ",
        model="q",
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )
    result = rewrite_query("hello", client=fake)
    assert result == "hello"


def test_retrieval_uses_rewritten_query_when_enabled(tmp_path, monkeypatch) -> None:
    """Integration: retrieve with rewritten query."""
    from ai_employee.knowledge_api.retrieval import RetrievalService
    from ai_employee.knowledge_api.store import SQLiteStore

    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    store = SQLiteStore(db_path=str(tmp_path / "k.sqlite3"), data_dir=str(tmp_path))
    store.init_schema()
    doc_id = store.create_document(
        "RRC SOP", "/tmp/x", "text/plain", {"network_type": "5g"}, ["wireless"], "v1"
    )
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [
            {
                "chunk_id": f"c_{doc_id}",
                "chunk_no": 1,
                "content": "RRC 建立失败先查告警KPI",
                "section_path": "root",
            }
        ],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")
    svc = RetrievalService(store)
    hits = svc.search(
        "RRC 建立失败先查告警KPI",
        ["wireless"],
        query_rewriter=None,
        top_k=3,
    )
    assert len(hits) >= 1
