"""Langfuse emitter usage / latency correctness tests (R24-B.4).

The previous emitter conflated ``latency_ms`` with the ``usage.total``
slot and labelled everything as ``MILLISECONDS`` — fine for a single
int metric but not honest token accounting.  These tests verify:

* when ``usage`` is supplied the emitter forwards it verbatim with
  ``unit=TOKENS``;
* when ``usage`` is missing the slot defaults to ``0`` (no latency
  smearing);
* ``latency_ms`` is preserved as a top-level metadata field so
  dashboards still see it.
"""

from __future__ import annotations

import json

import pytest
from ai_employee.observability.langfuse_emitter import LangfuseEmitter


class _FakeResponse:
    def __init__(self, status_code: int = 207) -> None:
        self.status_code = status_code
        self.text = "{}"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, *, headers: dict, content: str, timeout: float):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, "content": content, "headers": headers, "timeout": timeout})
        return _FakeResponse()


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


def _emitter(client: _FakeClient) -> LangfuseEmitter:
    return LangfuseEmitter(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        host="https://langfuse.example.com",
        client=client,  # type: ignore[arg-type]
    )


def _read_first_record(client: _FakeClient) -> dict:
    assert client.calls, "emitter did not flush"
    body = json.loads(client.calls[0]["content"])
    assert "batch" in body and len(body["batch"]) >= 1
    return body["batch"][0]


def test_record_forwards_real_token_usage(client: _FakeClient) -> None:
    """``usage`` is forwarded as TOKENS (no latency smearing)."""
    emitter = _emitter(client)
    emitter.record_llm_call(
        trace_id="t1",
        span_id="s1",
        model="qwen-plus",
        prompt="hi",
        response="hello",
        latency_ms=842.0,
        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    )
    emitter.flush()
    record = _read_first_record(client)
    usage = record["body"]["usage"]
    assert usage == {
        "unit": "TOKENS",
        "input": 11,
        "output": 7,
        "total": 18,
    }
    # Latency is no longer stuffed into the usage payload.
    assert "total" not in usage or usage["total"] != 842


def test_record_defaults_to_zero_usage_when_missing(client: _FakeClient) -> None:
    """No usage provided → 0/0/0 (unit=TOKENS), not a fake latency total."""
    emitter = _emitter(client)
    emitter.record_llm_call(
        trace_id="t2",
        span_id="s2",
        model="qwen-plus",
        prompt="hi",
        response="hello",
        latency_ms=842.0,
    )
    emitter.flush()
    record = _read_first_record(client)
    usage = record["body"]["usage"]
    assert usage == {
        "unit": "TOKENS",
        "input": 0,
        "output": 0,
        "total": 0,
    }


def test_record_preserves_latency_in_metadata(client: _FakeClient) -> None:
    """Latency still surfaces via metadata so dashboards see it."""
    emitter = _emitter(client)
    emitter.record_llm_call(
        trace_id="t3",
        span_id="s3",
        model="qwen-plus",
        prompt="hi",
        response="hello",
        latency_ms=842.0,
        metadata={"status": "succeeded"},
    )
    emitter.flush()
    record = _read_first_record(client)
    meta = record["body"]["metadata"]
    assert meta["status"] == "succeeded"
    assert meta["latency_ms"] == pytest.approx(842.0)


def test_record_no_usage_with_status_metadata(client: _FakeClient) -> None:
    """When usage is missing, status metadata still flows through."""
    emitter = _emitter(client)
    emitter.record_llm_call(
        trace_id="t4",
        span_id="s4",
        model="qwen-plus",
        prompt="hi",
        response="failed",
        latency_ms=12.0,
        metadata={"status": "failed"},
    )
    emitter.flush()
    record = _read_first_record(client)
    usage = record["body"]["usage"]
    meta = record["body"]["metadata"]
    assert usage["total"] == 0
    assert meta["status"] == "failed"
    assert meta["latency_ms"] == pytest.approx(12.0)
