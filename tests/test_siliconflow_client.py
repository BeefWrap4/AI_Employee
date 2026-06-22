"""SiliconFlow LLM client tests (R15-1).

SiliconFlow provides an OpenAI-compatible chat-completions endpoint at
``https://api.siliconflow.cn/v1``.  The :class:`SiliconFlowClient`
shortcut in ``llm_gateway`` honours the ``SILICONFLOW_API_KEY`` env var
and defaults to the platform's preferred Qwen model.  When the env is
unset, ``build_siliconflow_client`` raises so callers fail fast (no
silent fallback to another vendor).
"""

from __future__ import annotations

import pytest
from ai_employee.llm_gateway.client import (
    LlmClient,
    SiliconFlowClient,
    build_siliconflow_client,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


def test_siliconflow_base_url_constant() -> None:
    from ai_employee.llm_gateway.client import _SILICONFLOW_BASE_URL

    assert _SILICONFLOW_BASE_URL == "https://api.siliconflow.cn/v1"


def test_siliconflow_default_model_is_qwen() -> None:
    """Default to Qwen2.5-7B-Instruct — small, fast, instruction-tuned."""
    from ai_employee.llm_gateway.client import _SILICONFLOW_DEFAULT_MODEL

    assert "Qwen" in _SILICONFLOW_DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Direct construction
# --------------------------------------------------------------------------- #


def test_siliconflow_client_uses_explicit_key() -> None:
    client = SiliconFlowClient(api_key="sk-sf-test")
    assert client.base_url == "https://api.siliconflow.cn/v1"
    assert client.api_key == "sk-sf-test"
    assert "Qwen" in client.model


def test_siliconflow_client_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-from-env")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    client = SiliconFlowClient()
    assert client.api_key == "sk-from-env"


def test_siliconflow_client_can_override_model() -> None:
    client = SiliconFlowClient(
        api_key="sk-x",
        model="Qwen/Qwen2.5-72B-Instruct",
    )
    assert client.model == "Qwen/Qwen2.5-72B-Instruct"


def test_siliconflow_client_is_subclass_of_llm_client() -> None:
    """Inherits retry/Langfuse tracing from the base gateway."""
    client = SiliconFlowClient(api_key="sk-x")
    assert isinstance(client, LlmClient)


# --------------------------------------------------------------------------- #
# Factory: build_siliconflow_client
# --------------------------------------------------------------------------- #


def test_build_siliconflow_client_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-build")
    client = build_siliconflow_client()
    assert isinstance(client, SiliconFlowClient)
    assert client.api_key == "sk-build"


def test_build_siliconflow_client_missing_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail fast — no silent fallback to a different vendor."""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_BASE_URL", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        build_siliconflow_client()
    assert "SILICONFLOW_API_KEY" in str(exc_info.value)


def test_build_siliconflow_client_with_explicit_key() -> None:
    client = build_siliconflow_client(api_key="sk-explicit")
    assert client.api_key == "sk-explicit"


def test_build_siliconflow_client_honors_base_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow override for tests/proxies that point at a local mock."""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    monkeypatch.setenv("SILICONFLOW_BASE_URL", "http://localhost:9999/v1")
    client = build_siliconflow_client()
    assert client.base_url == "http://localhost:9999/v1"


# --------------------------------------------------------------------------- #
# chat() — uses retry decorator so an injected fake works
# --------------------------------------------------------------------------- #


def test_chat_parses_openai_compatible_response() -> None:
    """Mock the HTTP layer; verify request shape + response parsing."""
    from unittest.mock import patch

    fake_response = {
        "id": "chatcmpl-1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "你好"}},
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
        },
    }

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return fake_response

    class _HttpxPost:
        called = False

        @staticmethod
        def __call__(
            url: str,
            headers: dict,
            json: dict,
            timeout: float,
        ) -> _Resp:
            _HttpxPost.called = True
            # Verify the URL targets the SiliconFlow endpoint.
            assert url.startswith("https://api.siliconflow.cn/v1/chat/completions")
            # Verify the Authorization header carries the key.
            assert headers["Authorization"] == "Bearer sk-x"
            # Verify the model is set in the request body.
            assert json["model"] == "Qwen/Qwen2.5-7B-Instruct"
            return _Resp()

    client = SiliconFlowClient(api_key="sk-x")
    # Patch the module-level httpx reference.
    with patch("ai_employee.llm_gateway.client.httpx.post", new=_HttpxPost()):
        resp = client.chat(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0,
            max_tokens=64,
        )
    assert _HttpxPost.called
    assert resp.content == "你好"
    assert resp.usage["total_tokens"] == 16


def test_chat_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self) -> dict:
            return {}

    with patch(
        "ai_employee.llm_gateway.client.httpx.post",
        return_value=_Resp(),
    ):
        client = SiliconFlowClient(api_key="bad")
        with pytest.raises(Exception) as exc_info:
            client.chat(messages=[{"role": "user", "content": "x"}])
        # Should propagate as LlmClientError with status_code=401.
        assert "401" in str(exc_info.value) or "401" in repr(exc_info.value)
