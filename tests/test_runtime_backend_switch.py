"""RUNTIME_BACKEND switch tests (spec P3 §4 LangGraph v1).

Verifies the env-driven backend selection doesn't break the default
DAG path and that ``langgraph`` returns the LangGraph runtime.
"""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.runtime import select_runtime


def test_default_backend_returns_none_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (no env) → None sentinel → caller uses self-built DAG."""
    monkeypatch.delenv("RUNTIME_BACKEND", raising=False)
    assert select_runtime() is None


def test_dag_backend_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_BACKEND", "dag")
    assert select_runtime() is None


def test_langgraph_backend_returns_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    runtime = select_runtime()
    assert runtime is not None
    from ai_employee.agent_platform_api.langgraph_runtime import LangGraphRuntime
    assert isinstance(runtime, LangGraphRuntime)


def test_unknown_backend_falls_back_to_dag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_BACKEND", "unknown")
    assert select_runtime() is None


def test_switch_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_BACKEND", "LangGraph")
    runtime = select_runtime()
    assert runtime is not None
