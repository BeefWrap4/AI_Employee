"""Prompt template with {key}-style placeholder rendering."""

from __future__ import annotations

from string import Formatter
from typing import Any


class PromptTemplate:
    """A simple {key}-style string template.

    Parameters
    ----------
    system:
        System prompt text (optional).
    user:
        User prompt text with ``{key}`` placeholders.
    """

    def __init__(self, system: str = "", user: str = "") -> None:
        self.system = system
        self.user = user

    def render(self, **kwargs: str) -> dict[str, str]:
        """Render the template with the given keyword arguments.

        Returns a dict with ``"system"`` and ``"user"`` keys suitable for
        passing directly to :meth:`LlmClient.chat`.
        """
        rendered_user = self.user
        # Only try formatting if there are placeholders; .format would
        # raise KeyError on missing keys otherwise.
        fields = {f[1] for f in Formatter().parse(self.user) if f[1] is not None}
        if fields:
            # Supply empty-string defaults so callers can omit keys safely.
            defaults: dict[str, str] = {k: "" for k in fields}
            defaults.update(kwargs)
            rendered_user = self.user.format_map(defaults)
        return {
            "system": self.system,
            "user": rendered_user,
        }

    def to_messages(self, **kwargs: str) -> list[dict[str, str]]:
        """Render and return a messages list for the OpenAI chat-completions format."""
        rendered = self.render(**kwargs)
        messages: list[dict[str, str]] = []
        if rendered["system"]:
            messages.append({"role": "system", "content": rendered["system"]})
        messages.append({"role": "user", "content": rendered["user"]})
        return messages


# Pre-built RAG answer template.
RAG_ANSWER_TEMPLATE = PromptTemplate(
    system="你是基站运维专家，回答必须严谨、可追溯。",
    user="""根据以下证据回答问题。

证据:
{evidence}

问题: {question}

要求:
1. 答案必须基于上述证据，不得编造。
2. 每个结论需附带引用编号（如 [1]、[2]）。
3. 证据不足或矛盾时请明确说明。""",
)
