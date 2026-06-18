"""LLM Gateway -- OpenAI-compatible chat client for the AI Employee platform.

Provides:
- LlmClient: synchronous httpx client against any OpenAI-compatible endpoint
- PromptTemplate: simple {key} string-template with render(**kwargs)
- retry: exponential-backoff decorator for transient HTTP errors
"""

from ai_employee.llm_gateway.client import LlmClient
from ai_employee.llm_gateway.prompt import PromptTemplate
from ai_employee.llm_gateway.retry import retry

__all__ = ["LlmClient", "PromptTemplate", "retry"]
