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
from ai_employee.llm_gateway.model_registry import (
    ModelSpec,
    build_siliconflow_client_for_task,
    build_url_for_task,
    get_model_for_task,
    list_models,
    list_models_for_task,
)
from ai_employee.llm_gateway.prompt import PromptTemplate
from ai_employee.llm_gateway.retry import retry

__all__ = [
    "LlmClient",
    "ModelSpec",
    "PromptTemplate",
    "SiliconFlowClient",
    "build_siliconflow_client",
    "build_siliconflow_client_for_task",
    "build_url_for_task",
    "get_model_for_task",
    "list_models",
    "list_models_for_task",
    "retry",
]
