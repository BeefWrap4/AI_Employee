"""LLM Gateway -- OpenAI-compatible chat client for the AI Employee platform.

Provides:
- LlmClient: synchronous httpx client against any OpenAI-compatible endpoint
- SiliconFlowClient: shortcut pre-configured for 硅基流动 (api.siliconflow.cn)
- build_siliconflow_client: factory that fails fast when the key is missing
- PromptTemplate: simple {key} string-template with render(**kwargs)
- retry: exponential-backoff decorator for transient HTTP errors
"""

from ai_employee.llm_gateway.client import (
    LlmClient,
    SiliconFlowClient,
    build_siliconflow_client,
)
from ai_employee.llm_gateway.prompt import PromptTemplate
from ai_employee.llm_gateway.retry import retry

__all__ = [
    "LlmClient",
    "PromptTemplate",
    "SiliconFlowClient",
    "build_siliconflow_client",
    "retry",
]
