"""OpenAI-compatible chat-completions client.

Minimal synchronous wrapper around httpx that talks to any
OpenAI-compatible endpoint (DashScope, vLLM, Ollama, etc.).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx
from ai_employee.llm_gateway.retry import RetryExhaustedError
from ai_employee.llm_gateway.retry import retry as retry_decorator

_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "qwen-plus"
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
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", _DASHSCOPE_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("QWEN_API_KEY", "")
        self.model = model or os.getenv("LLM_MODEL", _DEFAULT_MODEL)
        self.timeout = timeout
        self.max_retries = max_retries

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

        try:
            resp = _do_request(messages, temperature, max_tokens)
        except Exception as exc:
            if isinstance(exc, RetryExhaustedError):
                raise LlmClientError(str(exc), status_code=exc.last_status) from exc
            raise LlmClientError(f"LLM request failed: {exc}") from exc

        if resp.status_code != 200:
            raise LlmClientError(
                f"unexpected status {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LlmClientError(f"invalid JSON response: {exc}") from exc

        choice = data["choices"][0]
        return ChatResponse(
            content=choice["message"]["content"],
            model=data.get("model", self.model),
            usage={
                "prompt_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "total_tokens": data.get("usage", {}).get("total_tokens", 0),
            },
        )
