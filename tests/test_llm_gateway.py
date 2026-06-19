"""Tests for the llm_gateway package: LlmClient, PromptTemplate, retry."""

from __future__ import annotations

from unittest import mock

import httpx
import pytest
from ai_employee.llm_gateway.client import ChatResponse, LlmClient, LlmClientError
from ai_employee.llm_gateway.prompt import RAG_ANSWER_TEMPLATE, PromptTemplate
from ai_employee.llm_gateway.retry import RetryExhaustedError, retry

# ---------------------------------------------------------------------------
# PromptTemplate tests
# ---------------------------------------------------------------------------


class TestPromptTemplate:
    def test_render_simple(self) -> None:
        t = PromptTemplate(system="You are helpful.", user="Hello {name}!")
        result = t.render(name="Alice")
        assert result == {"system": "You are helpful.", "user": "Hello Alice!"}

    def test_render_no_placeholders(self) -> None:
        t = PromptTemplate(system="sys", user="plain text")
        result = t.render()
        assert result == {"system": "sys", "user": "plain text"}

    def test_render_missing_key_uses_empty_string(self) -> None:
        t = PromptTemplate(system="", user="Q: {question} | Context: {context}")
        result = t.render(question="What is RRC?")
        assert result == {
            "system": "",
            "user": "Q: What is RRC? | Context: ",
        }

    def test_render_no_system(self) -> None:
        t = PromptTemplate(user="Answer: {answer}")
        result = t.render(answer="42")
        assert result == {"system": "", "user": "Answer: 42"}

    def test_to_messages(self) -> None:
        t = PromptTemplate(system="sys", user="hi {name}")
        msgs = t.to_messages(name="Bob")
        assert msgs == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi Bob"},
        ]

    def test_to_messages_no_system_omits_system_message(self) -> None:
        t = PromptTemplate(user="hi")
        msgs = t.to_messages()
        assert msgs == [{"role": "user", "content": "hi"}]

    def test_rag_template_renders(self) -> None:
        result = RAG_ANSWER_TEMPLATE.render(
            evidence="[1] RRC connection reject count > 100",
            question="Why is RRC failing?",
        )
        assert "基站运维专家" in result["system"]
        assert "[1] RRC" in result["user"]
        assert "Why is RRC failing?" in result["user"]

    def test_rag_template_missing_keys(self) -> None:
        """Keys missing from kwargs get empty-string defaults."""
        result = RAG_ANSWER_TEMPLATE.render(evidence="some evidence")
        assert "evidence" in result["user"]
        assert "问题: " in result["user"]
        assert "some evidence" in result["user"]

    def test_format_map_allows_extra_keys(self) -> None:
        t = PromptTemplate(user="{greeting}")
        result = t.render(greeting="hi", unused="ignored")
        assert result["user"] == "hi"


# ---------------------------------------------------------------------------
# LlmClient tests (mocked httpx)
# ---------------------------------------------------------------------------


def _fake_response(status_code: int, body: dict) -> httpx.Response:
    """Build a mock httpx.Response with the given status and JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "http://test/chat/completions"),
    )


class _RecordingEmitter:
    """Minimal fake LangfuseEmitter for the LlmClient integration test."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.flush_result = {"dispatched": 0}

    def record_llm_call(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)

    def flush(self) -> dict:
        return dict(self.flush_result)


class TestLlmClient:
    def test_chat_returns_chat_response(self) -> None:
        client = LlmClient(
            base_url="http://test",
            api_key="sk-test",
            model="test-model",
        )
        with mock.patch.object(
            httpx,
            "post",
            return_value=_fake_response(
                200,
                {
                    "choices": [{"message": {"content": "Hello!"}}],
                    "model": "test-model",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ),
        ):
            result = client.chat(
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                max_tokens=100,
            )
        assert isinstance(result, ChatResponse)
        assert result.content == "Hello!"
        assert result.model == "test-model"
        assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_chat_default_model_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MODEL", "qwen-turbo")
        monkeypatch.setenv("LLM_API_KEY", "sk-env")
        client = LlmClient(base_url="http://test")
        assert client.model == "qwen-turbo"

    def test_chat_fallback_to_qwen_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("QWEN_API_KEY", "sk-qwen")
        client = LlmClient(base_url="http://test")
        assert client.api_key == "sk-qwen"

    def test_chat_llm_api_key_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_API_KEY", "sk-llm")
        monkeypatch.setenv("QWEN_API_KEY", "sk-qwen")
        client = LlmClient(base_url="http://test")
        assert client.api_key == "sk-llm"

    def test_chat_raises_on_non_200(self) -> None:
        client = LlmClient(base_url="http://test", api_key="sk-test")
        with mock.patch.object(httpx, "post", return_value=_fake_response(500, {"error": "boom"})):
            with pytest.raises(LlmClientError) as exc_info:
                client.chat(messages=[{"role": "user", "content": "hi"}])
        assert "retries exhausted" in str(exc_info.value).lower() or "500" in str(exc_info.value)

    def test_chat_raises_on_invalid_json(self) -> None:
        client = LlmClient(base_url="http://test", api_key="sk-test")
        raw_resp = httpx.Response(
            status_code=200,
            content=b"not json",
            request=httpx.Request("POST", "http://test/chat/completions"),
        )
        with mock.patch.object(httpx, "post", return_value=raw_resp):
            with pytest.raises(LlmClientError) as exc_info:
                client.chat(messages=[{"role": "user", "content": "hi"}])
        assert (
            "invalid JSON" in str(exc_info.value).lower() or "json" in str(exc_info.value).lower()
        )

    def test_chat_strips_trailing_slash_from_base_url(self) -> None:
        client = LlmClient(base_url="http://test/", api_key="sk-test")
        with mock.patch.object(httpx, "post") as mock_post:
            mock_post.return_value = _fake_response(
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )
            client.chat(messages=[{"role": "user", "content": "hi"}])
            url_called = mock_post.call_args[0][0]
            assert not url_called.endswith("//chat/completions")


# ---------------------------------------------------------------------------
# retry decorator tests
# ---------------------------------------------------------------------------


def _echo_status(status: int) -> httpx.Response:
    """Test helper: returns a mock response with the given status."""
    return httpx.Response(
        status_code=status,
        json={"status": status},
        request=httpx.Request("POST", "http://test/"),
    )


class TestLlmClientLangfuse:
    """Langfuse trace emission integration with LlmClient."""

    def test_chat_records_to_emitter_when_provided(self) -> None:
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
                200,
                {
                    "choices": [{"message": {"content": "Hello!"}}],
                    "model": "qwen-plus",
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                },
            ),
        ):
            client.chat(messages=[{"role": "user", "content": "hi"}])
        assert len(emitter.calls) == 1
        record = emitter.calls[0]
        assert record["model"] == "qwen-plus"
        assert record["prompt"] == "hi"
        assert record["response"] == "Hello!"
        assert record["latency_ms"] >= 0
        # Trace/span ids should be valid hex.
        assert len(record["trace_id"]) == 32
        assert len(record["span_id"]) == 16

    def test_chat_no_emitter_is_fine(self) -> None:
        client = LlmClient(base_url="http://test", api_key="sk-test", model="qwen-plus")
        with mock.patch.object(
            httpx,
            "post",
            return_value=_fake_response(
                200,
                {"choices": [{"message": {"content": "ok"}}], "usage": {}},
            ),
        ):
            # Should not raise when no emitter is provided.
            result = client.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "ok"

    def test_chat_records_failure_to_emitter(self) -> None:
        emitter = _RecordingEmitter()
        client = LlmClient(
            base_url="http://test",
            api_key="sk-test",
            model="qwen-plus",
            langfuse_emitter=emitter,  # type: ignore[arg-type]
            max_retries=0,
        )
        with mock.patch.object(
            httpx,
            "post",
            side_effect=httpx.ConnectError("boom"),
        ):
            with pytest.raises(LlmClientError):
                client.chat(messages=[{"role": "user", "content": "hi"}])
        # Failure path: still record a trace marked as failed.
        assert len(emitter.calls) == 1
        assert emitter.calls[0].get("metadata", {}).get("status") == "failed"

    def test_emit_trace_redacts_phone_from_prompt(self) -> None:
        """Phones in the prompt must be masked before being sent to the trace."""
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
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "model": "qwen-plus",
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                },
            ),
        ):
            client.chat(
                messages=[{"role": "user", "content": "call 13800138000 now"}],
            )
        assert len(emitter.calls) == 1
        prompt_recorded = emitter.calls[0]["prompt"]
        assert "13800138000" not in prompt_recorded
        assert "***" in prompt_recorded

    def test_emit_trace_redacts_phone_from_response(self) -> None:
        """Phones in the LLM response must be masked in the trace."""
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
                200,
                {
                    "choices": [{"message": {"content": "Contact 13800138000 for help"}}],
                    "model": "qwen-plus",
                    "usage": {},
                },
            ),
        ):
            client.chat(messages=[{"role": "user", "content": "give me a hotline"}])
        assert len(emitter.calls) == 1
        response_recorded = emitter.calls[0]["response"]
        assert "13800138000" not in response_recorded
        assert "***" in response_recorded

    def test_emit_trace_redacts_email(self) -> None:
        """Emails should also be masked in the trace."""
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
                200,
                {
                    "choices": [{"message": {"content": "reply to admin@example.com"}}],
                    "model": "qwen-plus",
                    "usage": {},
                },
            ),
        ):
            client.chat(messages=[{"role": "user", "content": "ping admin@example.com"}])
        assert len(emitter.calls) == 1
        assert "admin@example.com" not in emitter.calls[0]["prompt"]
        assert "admin@example.com" not in emitter.calls[0]["response"]


class TestRetry:
    def test_retry_on_429_succeeds_eventually(self) -> None:
        call_count = [0]

        @retry(max_retries=3, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            if call_count[0] < 3:
                return _echo_status(429)
            return _echo_status(200)

        resp = do_request()
        assert resp.status_code == 200
        assert call_count[0] == 3

    def test_retry_on_5xx_succeeds(self) -> None:
        call_count = [0]

        @retry(max_retries=2, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            if call_count[0] < 3:
                return _echo_status(502)
            return _echo_status(200)

        resp = do_request()
        assert resp.status_code == 200
        assert call_count[0] == 3

    def test_no_retry_on_200(self) -> None:
        call_count = [0]

        @retry(max_retries=3, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            return _echo_status(200)

        resp = do_request()
        assert resp.status_code == 200
        assert call_count[0] == 1

    def test_give_up_on_401_immediately(self) -> None:
        call_count = [0]

        @retry(max_retries=3, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            return _echo_status(401)

        with pytest.raises(RetryExhaustedError) as exc_info:
            do_request()
        assert exc_info.value.last_status == 401
        assert call_count[0] == 1

    def test_give_up_on_403_immediately(self) -> None:
        call_count = [0]

        @retry(max_retries=3, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            return _echo_status(403)

        with pytest.raises(RetryExhaustedError) as exc_info:
            do_request()
        assert exc_info.value.last_status == 403
        assert call_count[0] == 1

    def test_retry_exhausted_on_429(self) -> None:
        call_count = [0]

        @retry(max_retries=2, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            return _echo_status(429)

        with pytest.raises(RetryExhaustedError) as exc_info:
            do_request()
        assert exc_info.value.last_status == 429
        assert call_count[0] == 3  # initial + 2 retries

    def test_retry_exhausted_on_5xx(self) -> None:
        call_count = [0]

        @retry(max_retries=1, base_delay=0.01)
        def do_request() -> httpx.Response:
            call_count[0] += 1
            return _echo_status(503)

        with pytest.raises(RetryExhaustedError) as exc_info:
            do_request()
        assert exc_info.value.last_status == 503
        assert call_count[0] == 2  # initial + 1 retry
