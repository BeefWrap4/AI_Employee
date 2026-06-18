"""OpenAI-compatible chat-completions client.

Minimal synchronous wrapper around httpx that talks to any
OpenAI-compatible endpoint (DashScope, vLLM, Ollama, etc.).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from ai_employee.llm_gateway.retry import RetryExhaustedError
from ai_employee.llm_gateway.retry import retry as retry_decorator

if TYPE_CHECKING:  # pragma: no cover
    from ai_employee.observability.langfuse_emitter import LangfuseEmitter

_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_MODEL = "qwen-plus"
_SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
_DEFAULT_MAX_TOKENS = 1024


@dataclass
class ChatResponse:
    """Normalised chat completion response."""

    content: str
    model: str
    usage: dict[str, int]  # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}


class LlmClientError(RuntimeError):
    """Raised when the LLM gateway encounters an unrecoverable error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LlmClient:
    """OpenAI-compatible chat-completions client."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        langfuse_emitter: LangfuseEmitter | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", _DASHSCOPE_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("QWEN_API_KEY", "")
        self.model = model or os.getenv("LLM_MODEL", _DEFAULT_MODEL)
        self.timeout = timeout
        self.max_retries = max_retries
        # Langfuse trace emitter is optional; when None, chat() skips tracing.
        self.langfuse_emitter = langfuse_emitter

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _raw_request(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> httpx.Response:
        """Single HTTP call — decorated separately so retry wraps it."""
        return httpx.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=self.timeout,
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> ChatResponse:
        """Send a chat-completion request.

        Parameters
        ----------
        messages:
            List of dicts with "role" and "content" keys.
        temperature:
            Sampling temperature (default 0.0).
        max_tokens:
            Maximum tokens to generate (default 1024).

        Returns
        -------
        ChatResponse
            Normalised response with content, model, and usage info.
        """
        _do_request = retry_decorator(max_retries=self.max_retries)(self._raw_request)

        # Emit a Langfuse trace record (success or failure) when an emitter is
        # configured.  Generates fresh trace/span ids so each chat() is its
        # own trace.  Tracing must never break the underlying call.
        from ai_employee.observability import new_span_id, new_trace_id

        trace_id = new_trace_id()
        span_id = new_span_id()
        prompt_text = "\n".join(
            m.get("content", "") for m in messages if m.get("role") == "user"
        ) or json.dumps(messages, ensure_ascii=False)
        started = time.perf_counter()
        status_label = "succeeded"
        try:
            resp = _do_request(messages, temperature, max_tokens)
        except Exception as exc:
            status_label = "failed"
            self._emit_trace(
                trace_id=trace_id,
                span_id=span_id,
                prompt=prompt_text,
                response=str(exc),
                latency_ms=(time.perf_counter() - started) * 1000.0,
                status=status_label,
            )
            if isinstance(exc, RetryExhaustedError):
                raise LlmClientError(str(exc), status_code=exc.last_status) from exc
            raise LlmClientError(f"LLM request failed: {exc}") from exc

        if resp.status_code != 200:
            status_label = "failed"
            self._emit_trace(
                trace_id=trace_id,
                span_id=span_id,
                prompt=prompt_text,
                response=f"http {resp.status_code}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                status=status_label,
            )
            raise LlmClientError(
                f"unexpected status {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            status_label = "failed"
            self._emit_trace(
                trace_id=trace_id,
                span_id=span_id,
                prompt=prompt_text,
                response=f"invalid json: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                status=status_label,
            )
            raise LlmClientError(f"invalid JSON response: {exc}") from exc

        choice = data["choices"][0]
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._emit_trace(
            trace_id=trace_id,
            span_id=span_id,
            prompt=prompt_text,
            response=choice["message"]["content"],
            latency_ms=latency_ms,
            status=status_label,
        )
        return ChatResponse(
            content=choice["message"]["content"],
            model=data.get("model", self.model),
            usage={
                "prompt_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "total_tokens": data.get("usage", {}).get("total_tokens", 0),
            },
        )

    def _emit_trace(
        self,
        *,
        trace_id: str,
        span_id: str,
        prompt: str,
        response: str,
        latency_ms: float,
        status: str,
    ) -> None:
        """Push one record to the Langfuse emitter (best-effort)."""
        if self.langfuse_emitter is None:
            return
        try:
            self.langfuse_emitter.record_llm_call(
                trace_id=trace_id,
                span_id=span_id,
                model=self.model,
                prompt=prompt,
                response=response,
                latency_ms=latency_ms,
                metadata={"status": status},
            )
        except Exception:
            # Tracing must never break a chat() call.
            pass


# --------------------------------------------------------------------------- #
# SiliconFlow (硅基流动) shortcut
# --------------------------------------------------------------------------- #


class SiliconFlowClient(LlmClient):
    """OpenAI-compatible client pre-configured for 硅基流动.

    Inherits retry / Langfuse tracing from :class:`LlmClient`.  Reads
    ``SILICONFLOW_API_KEY`` and ``SILICONFLOW_BASE_URL`` from the
    environment; explicit constructor args override the env.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        langfuse_emitter: LangfuseEmitter | None = None,
    ) -> None:
        resolved_base = (
            base_url
            or os.getenv("SILICONFLOW_BASE_URL")
            or _SILICONFLOW_BASE_URL
        )
        resolved_key = (
            api_key
            or os.getenv("SILICONFLOW_API_KEY")
            or os.getenv("LLM_API_KEY")
            or ""
        )
        resolved_model = (
            model
            or os.getenv("SILICONFLOW_MODEL")
            or _SILICONFLOW_DEFAULT_MODEL
        )
        super().__init__(
            base_url=resolved_base,
            api_key=resolved_key,
            model=resolved_model,
            timeout=timeout,
            max_retries=max_retries,
            langfuse_emitter=langfuse_emitter,
        )


def build_siliconflow_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
    max_retries: int = 2,
    langfuse_emitter: LangfuseEmitter | None = None,
) -> SiliconFlowClient:
    """Build a SiliconFlow client; raise if no API key is configured.

    Unlike :class:`LlmClient` (which silently defaults to DashScope),
    this factory fails fast when ``SILICONFLOW_API_KEY`` is missing —
    a deliberate design choice to prevent accidental cross-vendor
    usage during the demo / Alibaba Cloud deployment.
    """
    resolved_key = (
        api_key
        or os.getenv("SILICONFLOW_API_KEY")
        or os.getenv("LLM_API_KEY")
    )
    if not resolved_key:
        raise RuntimeError(
            "SILICONFLOW_API_KEY is not set; refusing to build "
            "SiliconFlowClient. Set SILICONFLOW_API_KEY in the "
            "environment or pass api_key explicitly.",
        )
    return SiliconFlowClient(
        base_url=base_url,
        api_key=resolved_key,
        model=model,
        timeout=timeout,
        max_retries=max_retries,
        langfuse_emitter=langfuse_emitter,
    )
