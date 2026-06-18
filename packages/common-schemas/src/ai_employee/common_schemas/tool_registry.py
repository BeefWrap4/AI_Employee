"""Lightweight MCP-compatible tool registry protocol.

Defines ``ToolSpec``, ``ToolHandler``, and ``ToolRegistry`` — the
minimum needed to:

1. Register a tool with a JSON-Schema-shaped input/output contract.
2. Look up tools by name and invoke them with validated arguments.
3. Serialise the registry to JSON for MCP transport (list tools) or
   agent platform runtime consumption.

This deliberately does not depend on the official ``mcp`` Python SDK
so the spec can be exercised in unit tests without an extra dependency.
The JSON shape is compatible with ``tools/list`` responses from
real MCP servers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

JsonDict = dict[str, Any]


class ToolNotFound(KeyError):
    """Raised when ``ToolRegistry.invoke`` is called with an unknown name."""


class ToolInvocationError(Exception):
    """Raised when a handler raises or returns the wrong shape."""


@dataclass
class ToolSpec:
    """Describes a single tool for the registry and MCP transport."""

    name: str
    description: str
    input_schema: JsonDict
    output_schema: JsonDict
    risk_level: str = "readonly"  # spec §5.3: readonly | suggest | approval_required | forbidden
    handler: Callable[..., JsonDict] | None = field(default=None, repr=False)
    service_name: str | None = None
    version: str = "v1"
    # Governance fields (spec §5.3 tool metadata).
    timeout_ms: int = 5000
    retry_policy: JsonDict = field(default_factory=lambda: {"max_retries": 0})
    health_check_url: str | None = None

    def to_mcp_tool(self) -> JsonDict:
        """Render as an MCP ``tools/list`` entry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "version": self.version,
            "metadata": {
                "risk_level": self.risk_level,
                "service_name": self.service_name,
                "timeout_ms": self.timeout_ms,
                "retry_policy": self.retry_policy,
                "health_check_url": self.health_check_url,
            },
        }


class ToolRegistry:
    """In-memory registry of :class:`ToolSpec` instances."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # -- registration -----------------------------------------------------

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        if not replace and spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- read accessors ---------------------------------------------------

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolNotFound(name)
        return self._tools[name]

    def list(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def list_by_service(self, service_name: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.service_name == service_name]

    def to_mcp_list(self) -> JsonDict:
        """Render as an MCP ``tools/list`` response payload."""
        return {
            "tools": [spec.to_mcp_tool() for spec in self._tools.values()],
        }

    # -- invocation -------------------------------------------------------

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> JsonDict:
        spec = self.get(name)
        if spec.handler is None:
            raise ToolInvocationError(f"tool has no handler: {name}")
        try:
            result = spec.handler(**(arguments or {}))
        except TypeError as exc:
            raise ToolInvocationError(f"bad arguments for {name}: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            raise ToolInvocationError(f"tool {name} failed: {exc}") from exc
        if not isinstance(result, Mapping):
            raise ToolInvocationError(
                f"tool {name} must return a mapping, got {type(result).__name__}"
            )
        return dict(result)


def make_json_schema(
    *,
    properties: JsonDict,
    required: list[str] | None = None,
    description: str | None = None,
) -> JsonDict:
    """Convenience constructor for a JSON Schema object."""
    schema: JsonDict = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def to_json(payload: Any) -> str:
    """Render a payload as compact JSON for MCP transport."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = [
    "ToolInvocationError",
    "ToolNotFound",
    "ToolRegistry",
    "ToolSpec",
    "JsonDict",
    "make_json_schema",
    "to_json",
]
