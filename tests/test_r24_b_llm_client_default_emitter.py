"""LlmClient default-Langfuse-emitter wiring tests (R24-B).

When ``LANGFUSE_ENABLED`` is set, ``LlmClient()`` should construct the
default Langfuse emitter (no-op when the flag is off) so callers do not
need to wire ``langfuse_emitter`` by hand.  When the flag is unset the
client stays a no-op tracer so test / dev environments don't push to
the real Langfuse account.
"""
from __future__ import annotations

from unittest import mock

import httpx
import pytest
from ai_employee.llm_gateway.client import LlmClient


def _fake_response(body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "http://test/chat/completions"),
    )


def test_llm_client_uses_default_emitter_when_langfuse_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env=1 → emitter is non-None and enabled."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_default")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_default")
    client = LlmClient(base_url="http://test", api_key="sk-test")
    assert client.langfuse_emitter is not None
    assert client.langfuse_emitter.enabled is True


def test_llm_client_default_emitter_disabled_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env unset → emitter still built but disabled (no-op)."""
    for var in ("LANGFUSE_ENABLED", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    client = LlmClient(base_url="http://test", api_key="sk-test")
    # We always wire a default emitter; whether it dispatches is gated
    # by ``enabled``.  When env is unset the emitter is a no-op so the
    # caller sees the same behaviour as before this change.
    assert client.langfuse_emitter is not None
    assert client.langfuse_emitter.enabled is False


def test_llm_client_explicit_emitter_overrides_default() -> None:
    """Caller-supplied emitter wins over the default builder."""

    class _Sentinel:
        enabled = True
        calls: list = []

        def record_llm_call(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.calls.append(kwargs)

    sentinel = _Sentinel()
    client = LlmClient(
        base_url="http://test",
        api_key="sk-test",
        langfuse_emitter=sentinel,  # type: ignore[arg-type]
    )
    assert client.langfuse_emitter is sentinel


def test_default_emitter_records_chat_when_env_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env=1 → chat() actually buffers a trace."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    # Replace the underlying http client so flush() doesn't try to hit
    # the real Langfuse endpoint.
    fake_client = mock.MagicMock()
    fake_client.post.return_value = mock.MagicMock(status_code=207, text="{}")
    client = LlmClient(base_url="http://test", api_key="sk-test")
    client.langfuse_emitter.client = fake_client  # type: ignore[attr-defined]

    with mock.patch.object(
        httpx,
        "post",
        return_value=_fake_response(
            {
                "choices": [{"message": {"content": "hi"}}],
                "model": "test",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ),
    ):
        client.chat([{"role": "user", "content": "hello"}])
    result = client.langfuse_emitter.flush()
    assert result["dispatched"] == 1
