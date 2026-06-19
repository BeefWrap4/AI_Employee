"""knowledge-api trace emission tests (R24-B.2).

When ``LLM_GATEWAY_ENABLED`` is on and the langfuse env is configured,
the query path must:

* reuse the request-scoped trace id (``trace_<session>_query``) across
  every ``chat()`` call so multiple LLM hops surface as one Langfuse
  trace;
* pick up the process-wide default emitter (R24-B.1) instead of
  requiring the caller to inject one explicitly.

We mock ``httpx.post`` (so the LLM gateway never actually calls
DashScope) and the Langfuse emitter's http client (so flush() doesn't
hit the real endpoint).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx
import pytest
from ai_employee.knowledge_api.app import create_app
from ai_employee.llm_gateway.client import LlmClient
from ai_employee.observability.langfuse_emitter import LangfuseEmitter
from fastapi.testclient import TestClient


def _fake_response(body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "http://test/chat/completions"),
    )


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, *, headers: dict, content: str, timeout: float):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, "content": content, "headers": headers, "timeout": timeout})

        class _Resp:
            status_code = 207
            text = "{}"

        return _Resp()


class _CapturingLlmClient:
    """Fake ``LlmClient`` that records ``parent_trace_id`` and returns a stub.

    Replaces the real client so the test can assert the parent_trace_id
    wiring without depending on httpx or Langfuse end-to-end.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[dict] = []

    def chat(self, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        return SimpleNamespace(
            content="stubbed answer",
            model="captured",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        )


def _enable_llm_gateway(
    monkeypatch, fake_langfuse_client: _FakeLangfuseClient | None = None
) -> _CapturingLlmClient:
    """Patch the LlmClient symbol that app.py imports lazily.

    app.py does ``from ai_employee.llm_gateway.client import LlmClient`` at the
    call site, so we must patch the binding on the source module.  We also
    flip the module-level ``_LLM_GATEWAY_ENABLED`` flag because the app
    reads it at import-time.
    """
    import ai_employee.knowledge_api.app as app_module
    import ai_employee.llm_gateway.client as client_module

    capturing = _CapturingLlmClient()
    monkeypatch.setattr(client_module, "LlmClient", lambda *a, **kw: capturing)
    monkeypatch.setattr(app_module, "_LLM_GATEWAY_ENABLED", True)
    if fake_langfuse_client is not None:
        # Patch LangfuseEmitter.__init__ so every emitter built in the
        # test (including the one the capturing client would build)
        # has its http client replaced with the fake.
        original_init = LangfuseEmitter.__init__

        def _patched(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            original_init(self, *args, **kwargs)
            self.client = fake_langfuse_client  # type: ignore[assignment]

        monkeypatch.setattr(LangfuseEmitter, "__init__", _patched)
    return capturing


def _seed_published_chunk(
    tmp_path, store_path: str, doc_title: str = "RRC SOP", content: str = "RRC 建立失败先查告警KPI"
) -> None:
    from ai_employee.knowledge_api.store import SQLiteStore

    store = SQLiteStore(db_path=store_path, data_dir=str(tmp_path))
    doc_id = store.create_document(
        doc_title, "/tmp/x", "text/plain", {"network_type": "5g"}, ["wireless"], "v1"
    )
    store.transition_status(doc_id, "parsing")
    store.write_chunks(
        doc_id,
        [
            {
                "chunk_id": f"c_{doc_id}",
                "chunk_no": 1,
                "content": content,
                "section_path": "root",
            }
        ],
        [[0.0] * 8],
        "stub",
    )
    store.transition_status(doc_id, "published")


# --------------------------------------------------------------------------- #
# Bare LlmClient() picks up the default emitter (R24-B.1 + R24-B.2)
# --------------------------------------------------------------------------- #


def test_llm_client_default_uses_langfuse_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare LlmClient() inside the knowledge-api uses the default emitter."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    # The bare constructor reads env at __init__ time.
    client = LlmClient(base_url="http://test", api_key="sk-test")
    assert client.langfuse_emitter is not None
    assert client.langfuse_emitter.enabled is True


def test_query_endpoint_passes_parent_trace_id_to_chat(
    knowledge_workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query path must hand ``trace_id`` to ``chat`` so the trace is shared."""
    capturing = _enable_llm_gateway(monkeypatch)
    app = create_app()
    client = TestClient(app)
    _seed_published_chunk(knowledge_workspace, str(knowledge_workspace / "knowledge.sqlite3"))
    response = client.post(
        "/api/v1/chat/query",
        json={"question": "什么是 RRC？", "session_id": "s_test", "knowledge_scopes": ["wireless"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "stubbed answer"
    assert body["trace_id"].startswith("trace_")
    # LlmClient.chat was called and received the request's trace id.
    assert len(capturing.calls) == 1
    assert capturing.calls[0]["kwargs"].get("parent_trace_id") == body["trace_id"]


def test_query_endpoint_emits_trace_via_default_emitter(
    knowledge_workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: real LlmClient → Langfuse emitter buffers + dispatches one trace."""
    fake_lf = _FakeLangfuseClient()
    # Patch LlmClient's __init__ so the bare constructor captures the
    # emitter used during the request — without the capture we can't
    # reach the per-request emitter from outside the call.
    captured_emitters: list[LangfuseEmitter] = []
    import ai_employee.llm_gateway.client as client_module

    original_init = client_module.LlmClient.__init__

    def _capturing_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, *args, **kwargs)
        captured_emitters.append(self.langfuse_emitter)

    monkeypatch.setattr(client_module.LlmClient, "__init__", _capturing_init)

    # Also patch LangfuseEmitter.__init__ so every emitter built uses
    # our fake http client (otherwise flush hits the real endpoint).
    original_lf_init = LangfuseEmitter.__init__

    def _patched_lf_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_lf_init(self, *args, **kwargs)
        self.client = fake_lf  # type: ignore[assignment]

    monkeypatch.setattr(LangfuseEmitter, "__init__", _patched_lf_init)

    # Make sure the env enables the emitter so the bare LlmClient
    # picks it up.
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_test")

    # The app reads ``_LLM_GATEWAY_ENABLED`` at module import time so
    # we must flip it explicitly for this test.
    import ai_employee.knowledge_api.app as app_module

    monkeypatch.setattr(app_module, "_LLM_GATEWAY_ENABLED", True)

    with mock.patch.object(
        httpx,
        "post",
        return_value=_fake_response(
            {
                "choices": [{"message": {"content": "stubbed answer"}}],
                "model": "qwen-plus",
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        ),
    ):
        app = create_app()
        client = TestClient(app)
        _seed_published_chunk(knowledge_workspace, str(knowledge_workspace / "knowledge.sqlite3"))
        response = client.post(
            "/api/v1/chat/query",
            json={
                "question": "什么是 RRC？",
                "session_id": "s_test",
                "knowledge_scopes": ["wireless"],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "stubbed answer"
    # Exactly one LlmClient was constructed during the request.
    assert len(captured_emitters) == 1
    emitter = captured_emitters[0]
    assert emitter.enabled is True
    # Buffer is non-empty — the chat() actually recorded a trace.
    with emitter._lock:  # type: ignore[attr-defined]
        assert len(emitter._buffer) >= 1  # type: ignore[attr-defined]
    # Now flush so the fake http client receives the batch.
    result = emitter.flush()
    assert result["dispatched"] == 1
    assert fake_lf.calls, "default emitter did not dispatch a record"
    batch = json.loads(fake_lf.calls[0]["content"])["batch"]
    # Langfuse nests trace id under ``body.traceId`` (not at the top
    # level).
    trace_id = batch[0].get("traceId") or batch[0]["body"]["traceId"]
    assert trace_id == body["trace_id"]
    usage = batch[0]["body"]["usage"]
    assert usage == {"unit": "TOKENS", "input": 5, "output": 3, "total": 8}
    assert "latency_ms" in batch[0]["body"]["metadata"]
