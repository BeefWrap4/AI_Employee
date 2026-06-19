"""LlmClient trace_id propagation tests (R24-B.3).

The previous implementation minted a fresh trace id on every ``chat()``
call which meant a multi-step agent run (e.g. query rewrite + RAG
answer) showed up as two unrelated traces in Langfuse.  When a caller
passes ``parent_trace_id`` the client must reuse it across multiple
``chat()`` calls so the whole run surfaces as one trace.
"""

from __future__ import annotations

from unittest import mock

import httpx
from ai_employee.llm_gateway.client import LlmClient


def _fake_response(body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "http://test/chat/completions"),
    )


class _RecordingEmitter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_llm_call(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)

    def flush(self) -> dict:
        return {"dispatched": len(self.calls)}


def test_chat_shares_trace_id_when_parent_supplied() -> None:
    """Two chat() calls with the same parent_trace_id share one trace."""
    emitter = _RecordingEmitter()
    client = LlmClient(
        base_url="http://test",
        api_key="sk-test",
        model="qwen-plus",
        langfuse_emitter=emitter,  # type: ignore[arg-type]
    )
    parent = "trace_run_abc123_shared_32chars_xx"
    with mock.patch.object(
        httpx,
        "post",
        return_value=_fake_response(
            {
                "choices": [{"message": {"content": "ok"}}],
                "model": "qwen-plus",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ),
    ) as m_post:
        client.chat(
            [{"role": "user", "content": "hi"}],
            parent_trace_id=parent,
        )
        client.chat(
            [{"role": "user", "content": "again"}],
            parent_trace_id=parent,
        )
    assert m_post.call_count == 2
    assert len(emitter.calls) == 2
    trace_ids = {call["trace_id"] for call in emitter.calls}
    assert trace_ids == {parent}


def test_chat_generates_fresh_trace_id_by_default() -> None:
    """Without parent_trace_id each call gets its own fresh trace."""
    emitter = _RecordingEmitter()
    client = LlmClient(
        base_url="http://test",
        api_key="sk-test",
        model="qwen-plus",
        langfuse_emitter=emitter,  # type: ignore[arg-type]
    )
    with mock.patch.object(
        httpx,
        "post",
        return_value=_fake_response(
            {
                "choices": [{"message": {"content": "ok"}}],
                "model": "qwen-plus",
                "usage": {},
            },
        ),
    ):
        client.chat([{"role": "user", "content": "a"}])
        client.chat([{"role": "user", "content": "b"}])
    trace_ids = {call["trace_id"] for call in emitter.calls}
    assert len(trace_ids) == 2


def test_chat_each_call_still_has_unique_span_id() -> None:
    """Span ids stay unique per call even when trace_id is shared."""
    emitter = _RecordingEmitter()
    client = LlmClient(
        base_url="http://test",
        api_key="sk-test",
        model="qwen-plus",
        langfuse_emitter=emitter,  # type: ignore[arg-type]
    )
    parent = "trace_shared_32chars_xxxxxxxxxxxxx"
    with mock.patch.object(
        httpx,
        "post",
        return_value=_fake_response(
            {
                "choices": [{"message": {"content": "ok"}}],
                "model": "qwen-plus",
                "usage": {},
            },
        ),
    ):
        client.chat([{"role": "user", "content": "a"}], parent_trace_id=parent)
        client.chat([{"role": "user", "content": "b"}], parent_trace_id=parent)
    span_ids = [call["span_id"] for call in emitter.calls]
    assert len(span_ids) == 2
    assert span_ids[0] != span_ids[1]
    # 16 hex chars per Langfuse spec.
    assert all(len(sid) == 16 for sid in span_ids)
