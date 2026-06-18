"""R19-1: 追问上下文注入 (multi-turn context injection) tests.

Verifies that ``/api/v1/chat/query`` injects prior session chunks + answers
into the LLM prompt as ``context_str`` so follow-up questions can resolve
pronouns / references against earlier turns.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient


class _CapturingClient:
    """Fake LlmClient that records every ``chat(messages)`` call."""

    def __init__(self) -> None:
        self.captured: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.captured.append([dict(m) for m in messages])
        return SimpleNamespace(
            content="ok",
            model="captured",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


def _enable_llm_gateway(monkeypatch, capturing: _CapturingClient) -> None:
    """Patch the LlmClient symbol that app.py imports lazily.

    app.py does ``from ai_employee.llm_gateway.client import LlmClient`` at the
    call site, so we must patch the binding on the *source* module
    (ai_employee.llm_gateway.client), which is what ``from ... import`` binds
    the name to.  We also flip the module-level ``_LLM_GATEWAY_ENABLED`` flag
    because the app reads it at import-time.
    """
    monkeypatch.setenv("LLM_GATEWAY_ENABLED", "true")
    import ai_employee.llm_gateway.client as client_module
    import ai_employee.knowledge_api.app as app_module

    def _factory(*args: Any, **kwargs: Any) -> _CapturingClient:
        return capturing

    monkeypatch.setattr(client_module, "LlmClient", _factory)
    monkeypatch.setattr(app_module, "_LLM_GATEWAY_ENABLED", True)


def _upload_and_publish(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/documents",
        data={
            "title": "RRC 排障 SOP",
            "metadata_json": json.dumps({"network_type": "5g"}),
            "acl_tags_json": json.dumps(["wireless"]),
            "version": "v1",
            "mime_type": "text/markdown",
        },
        files={
            "file": (
                "sop.md",
                "RRC 建立失败先检查告警 KPI 与传输链路。".encode("utf-8"),
                "text/markdown",
            )
        },
    )
    assert resp.status_code == 202, resp.text
    doc_id = resp.json()["doc_id"]
    pub = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert pub.status_code == 200
    return doc_id


def test_followup_includes_prior_chunks_and_answer_in_context_str(
    api_factory, monkeypatch
) -> None:
    client = api_factory()
    _upload_and_publish(client)
    capturing = _CapturingClient()
    _enable_llm_gateway(monkeypatch, capturing)

    # First turn — establishes session history with retrieved chunks + answer.
    r1 = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "r19-mt",
            "question": "RRC 建立失败先查什么",
            "knowledge_scopes": ["wireless"],
            "stream": False,
        },
    )
    assert r1.status_code == 200, r1.text
    trace_id_1 = r1.json()["trace_id"]

    # Second turn — the follow-up. Capture the prompt sent this turn.
    r2 = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "r19-mt",
            # Phrase includes anchor tokens so retrieval still finds the doc
            # even after the prior_turn_hint suffix is appended.
            "question": "那传输侧 KPI 链路呢",
            "knowledge_scopes": ["wireless"],
            "stream": False,
        },
    )
    assert r2.status_code == 200, r2.text
    assert len(capturing.captured) >= 2
    # The most recent capture is the second turn.
    messages = capturing.captured[-1]
    user_messages = [m for m in messages if m.get("role") == "user"]
    assert user_messages, "no user-role message captured"
    joined = "\n".join(m["content"] for m in user_messages)
    # Prior chunks (chunk_id of first turn) and prior answer text should be present
    # in the LLM context string injected into the prompt.
    qa_log_1 = client.get(f"/api/v1/qa-logs/{trace_id_1}").json()
    prior_chunk_ids = [c["chunk_id"] for c in qa_log_1["retrieved_chunks"]]
    assert prior_chunk_ids, "first turn produced no retrieved chunks"
    assert any(cid in joined for cid in prior_chunk_ids), (
        "prior chunk_id not referenced in context_str; got: " + joined[:400]
    )
    # Prior answer text should also appear in the context_str.
    assert qa_log_1["answer"] in joined or qa_log_1["answer"][:200] in joined, (
        "prior answer text not injected into context_str"
    )


def test_first_turn_has_no_context_str_prefix(api_factory, monkeypatch) -> None:
    """When no prior turn exists, no prior-context block is injected."""
    client = api_factory()
    _upload_and_publish(client)
    capturing = _CapturingClient()
    _enable_llm_gateway(monkeypatch, capturing)

    r = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "r19-mt-first",
            "question": "RRC 建立失败先查什么",
            "knowledge_scopes": ["wireless"],
            "stream": False,
        },
    )
    assert r.status_code == 200, r.text
    assert len(capturing.captured) == 1
    messages = capturing.captured[0]
    user_messages = [m for m in messages if m.get("role") == "user"]
    joined = "\n".join(m["content"] for m in user_messages)
    # No prior turn → no prior context block prefix
    assert "[历史上下文]" not in joined
    assert "[prior context]" not in joined.lower()