"""R28-PG: verify create_app wires PG backend when DATABASE_URL is set.

Pre-R28 the three services hardcoded SQLite/memory stores in create_app
and never called their build_*_store() factories — DATABASE_URL was
silently ignored in production. These tests pin the fix.
"""

from __future__ import annotations

import inspect


def test_knowledge_api_create_app_uses_build_knowledge_store() -> None:
    """create_app must delegate store construction to build_knowledge_store
    (which honours DATABASE_URL) rather than hardcoding SQLiteStore."""
    from ai_employee.knowledge_api import app as app_mod

    src = inspect.getsource(app_mod.create_app)
    assert "build_knowledge_store" in src, (
        "create_app must call build_knowledge_store() so DATABASE_URL is honoured"
    )


def test_rca_agent_default_store_uses_build_rca_store() -> None:
    """_default_store must delegate to build_rca_store() when DATABASE_URL
    is set, not just check RCA_SQLITE_PATH."""
    from ai_employee.rca_agent import app as app_mod

    src = inspect.getsource(app_mod._default_store)
    assert "build_rca_store" in src, (
        "_default_store must call build_rca_store() so DATABASE_URL is honoured"
    )


def test_agent_platform_create_app_uses_build_run_store() -> None:
    """create_app must delegate run_store construction to build_run_store()
    (which honours DATABASE_URL) rather than hardcoding AgentRunStore()."""
    from ai_employee.agent_platform_api import app as app_mod

    src = inspect.getsource(app_mod.create_app)
    assert "build_run_store" in src, (
        "create_app must call build_run_store() so DATABASE_URL is honoured"
    )
