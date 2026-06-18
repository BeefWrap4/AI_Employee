"""Langfuse LLM trace emitter tests.

The emitter buffers LLM call traces (model, prompt, response, latency,
metadata) and flushes them to Langfuse's HTTP ingestion endpoint.  When
LANGFUSE_ENABLED is false (the default) it short-circuits to a no-op so
tests / dev environments don't accidentally push to a real account.
"""
from __future__ import annotations

import json

import pytest

from ai_employee.observability.langfuse_emitter import (
    LangfuseEmitter,
    build_langfuse_emitter,
)


class FakeResponse:
    def __init__(self, status_code: int = 207, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {"success": []}
        self.text = json.dumps(self._body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeClient:
    def __init__(self, response: FakeResponse | None = None, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._response = response or FakeResponse()
        self._raise = raise_exc

    def post(self, url: str, *, headers: dict, content: str, timeout: float) -> FakeResponse:
        self.calls.append((url, {"headers": headers, "content": content, "timeout": timeout}))
        if self._raise is not None:
            raise self._raise
        return self._response


def test_disabled_emitter_is_noop() -> None:
    emitter = build_langfuse_emitter(
        enabled=False, public_key="pk", secret_key="sk", host="http://x",
    )
    assert emitter.enabled is False
    emitter.record_llm_call(
        trace_id="t1", span_id="s1", model="gpt-4o",
        prompt="hi", response="hello", latency_ms=12.0,
    )
    result = emitter.flush()
    assert result["dispatched"] == 0
    assert result.get("skipped") == "disabled"


def test_enabled_emitter_buffers_and_flushes() -> None:
    client = FakeClient()
    emitter = LangfuseEmitter(
        enabled=True, public_key="pk_test", secret_key="sk_test",
        host="https://langfuse.example.com", client=client,  # type: ignore[arg-type]
    )
    emitter.record_llm_call(
        trace_id="t1", span_id="s1", model="gpt-4o",
        prompt="hi", response="hello", latency_ms=120.0,
        metadata={"role": "user"},
    )
    emitter.record_llm_call(
        trace_id="t2", span_id="s2", model="claude",
        prompt="explain", response="...", latency_ms=900.0,
    )
    result = emitter.flush()
    assert result == {"dispatched": 2}
    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert url == "https://langfuse.example.com/api/public/ingestion"
    assert "Basic" in payload["headers"]["Authorization"]
    body = json.loads(payload["content"])
    assert "batch" in body
    assert len(body["batch"]) == 2
    # Verify trace ids round-trip.
    trace_ids = sorted(item["id"] for item in body["batch"])
    assert trace_ids == ["t1", "t2"]


def test_flush_handles_network_error() -> None:
    client = FakeClient(raise_exc=RuntimeError("connection refused"))
    emitter = LangfuseEmitter(
        enabled=True, public_key="pk", secret_key="sk",
        host="http://x", client=client,  # type: ignore[arg-type]
    )
    emitter.record_llm_call(
        trace_id="t1", span_id="s1", model="gpt-4o",
        prompt="hi", response="hello", latency_ms=12.0,
    )
    # Must not raise; dispatcher returns dispatched=0.
    result = emitter.flush()
    assert result["dispatched"] == 0
    assert result.get("error")


def test_flush_skips_when_buffer_empty() -> None:
    client = FakeClient()
    emitter = LangfuseEmitter(
        enabled=True, public_key="pk", secret_key="sk",
        host="http://x", client=client,  # type: ignore[arg-type]
    )
    result = emitter.flush()
    assert result == {"dispatched": 0}
    assert client.calls == []


def test_build_langfuse_emitter_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_env")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    emitter = build_langfuse_emitter()
    assert emitter.enabled is True
    assert emitter.public_key == "pk_env"
    assert emitter.secret_key == "sk_env"
    assert emitter.host == "https://cloud.langfuse.com"


def test_build_langfuse_emitter_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    emitter = build_langfuse_emitter()
    assert emitter.enabled is False
