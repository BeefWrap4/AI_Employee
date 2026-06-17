"""Tool registry protocol tests."""
from __future__ import annotations

import json

import pytest

from ai_employee.common_schemas.tool_registry import (
    ToolInvocationError,
    ToolNotFound,
    ToolRegistry,
    ToolSpec,
    make_json_schema,
    to_json,
)


def test_register_and_get_tool() -> None:
    reg = ToolRegistry()
    spec = ToolSpec(
        name="knowledge-api.chat.query",
        description="Query knowledge base",
        input_schema=make_json_schema(
            properties={"question": {"type": "string"}},
            required=["question"],
        ),
        output_schema=make_json_schema(
            properties={"answer": {"type": "string"}},
        ),
    )
    reg.register(spec)
    assert reg.get("knowledge-api.chat.query").name == "knowledge-api.chat.query"


def test_register_duplicate_raises() -> None:
    reg = ToolRegistry()
    spec = ToolSpec(
        name="dup", description="x",
        input_schema={}, output_schema={},
    )
    reg.register(spec)
    with pytest.raises(ValueError):
        reg.register(spec)
    reg.register(spec, replace=True)


def test_get_unknown_raises() -> None:
    reg = ToolRegistry()
    with pytest.raises(ToolNotFound):
        reg.get("does-not-exist")


def test_list_returns_all_tools() -> None:
    reg = ToolRegistry()
    reg.register(ToolSpec(name="a", description="x", input_schema={}, output_schema={}))
    reg.register(ToolSpec(name="b", description="y", input_schema={}, output_schema={}))
    assert sorted(t.name for t in reg.list()) == ["a", "b"]


def test_list_by_service_filters() -> None:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="k.q", description="x", input_schema={}, output_schema={},
        service_name="knowledge-api",
    ))
    reg.register(ToolSpec(
        name="r.r", description="y", input_schema={}, output_schema={},
        service_name="rca-agent",
    ))
    matches = reg.list_by_service("knowledge-api")
    assert [t.name for t in matches] == ["k.q"]


def test_invoke_runs_handler_with_arguments() -> None:
    reg = ToolRegistry()
    def handler(question: str, top_k: int = 3) -> dict:
        return {"answer": f"top {top_k} hits for {question}"}
    spec = ToolSpec(
        name="demo", description="x", input_schema={}, output_schema={},
        handler=handler,
    )
    reg.register(spec)
    result = reg.invoke("demo", {"question": "RRC", "top_k": 5})
    assert result == {"answer": "top 5 hits for RRC"}


def test_invoke_without_handler_raises() -> None:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="nohandler", description="x", input_schema={}, output_schema={},
    ))
    with pytest.raises(ToolInvocationError):
        reg.invoke("nohandler", {})


def test_invoke_with_bad_arguments_raises() -> None:
    reg = ToolRegistry()
    def handler(question: str) -> dict:
        return {"answer": question}
    reg.register(ToolSpec(
        name="badargs", description="x", input_schema={}, output_schema={},
        handler=handler,
    ))
    with pytest.raises(ToolInvocationError):
        reg.invoke("badargs", {"unknown": "x"})


def test_invoke_handler_returning_non_mapping_raises() -> None:
    reg = ToolRegistry()
    def handler() -> str:
        return "plain string"
    reg.register(ToolSpec(
        name="strreturn", description="x", input_schema={}, output_schema={},
        handler=handler,
    ))
    with pytest.raises(ToolInvocationError):
        reg.invoke("strreturn", {})


def test_to_mcp_list_renders_tool_payload() -> None:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="demo", description="desc",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level="read_only",
        service_name="knowledge-api",
    ))
    payload = reg.to_mcp_list()
    assert "tools" in payload
    tool = payload["tools"][0]
    assert tool["name"] == "demo"
    assert tool["inputSchema"]["type"] == "object"
    assert tool["metadata"]["risk_level"] == "read_only"
    assert tool["metadata"]["service_name"] == "knowledge-api"
    # Confirm the payload is JSON-serialisable.
    json.dumps(payload, ensure_ascii=False)


def test_to_json_handles_unicode_and_sorts_keys() -> None:
    payload = {"b": 1, "a": "你好"}
    out = to_json(payload)
    assert out == '{"a": "你好", "b": 1}'


def test_make_json_schema_builds_required_list() -> None:
    schema = make_json_schema(
        properties={"q": {"type": "string"}},
        required=["q"],
        description="query schema",
    )
    assert schema == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "description": "query schema",
    }
