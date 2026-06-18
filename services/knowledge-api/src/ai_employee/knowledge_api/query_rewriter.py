"""Search query rewriting — converts informal telecom questions into concise keyword queries.

Uses the already-available LLM gateway (LlmClient + PromptTemplate) to produce
a rewritten query string suitable for BM25 / FTS5 / vector recall.

This is a standalone module; no FastAPI dependency.
"""

from __future__ import annotations

from ai_employee.llm_gateway.client import LlmClient, LlmClientError
from ai_employee.llm_gateway.prompt import PromptTemplate

_QUERY_REWRITE_TEMPLATE = PromptTemplate(
    system="你是电信运维检索专家。",
    user="""将以下口语化问题改写为适合全文检索的简洁关键词组合。

原始问题: {question}

要求:
1. 提取核心关键词（告警码、网元、指标名、故障类型）。
2. 用空格分隔关键词，不要输出完整句子。
3. 保留原始问题中的重要术语。
4. 只输出改写后的关键词，不要解释。""",
)


def rewrite_query(
    question: str,
    *,
    client: LlmClient | None = None,
    fallback: bool = True,
) -> str:
    """Rewrite a natural-language question into keyword search terms.

    Parameters
    ----------
    question:
        Original question text.
    client:
        Optional pre-configured :class:`LlmClient`.  When omitted, a default
        client that reads ``LLM_BASE_URL`` / ``QWEN_API_KEY`` / ``LLM_MODEL``
        from the environment is created.
    fallback:
        When ``True`` and the LLM call fails (timeout, auth error, …), return
        the original *question* as-is so that retrieval still works (degraded
        but not broken).  When ``False``, let the exception propagate.

    Returns
    -------
    str
        Rewritten query string.
    """
    llm = client or LlmClient()
    try:
        messages = _QUERY_REWRITE_TEMPLATE.to_messages(question=question)
        resp = llm.chat(messages, temperature=0.0, max_tokens=128)
    except LlmClientError:
        if fallback:
            return question
        raise
    rewritten = resp.content.strip().rstrip(".").rstrip(",").rstrip("。").rstrip("，")
    return rewritten or question


__all__ = ["LlmClientError", "rewrite_query"]
