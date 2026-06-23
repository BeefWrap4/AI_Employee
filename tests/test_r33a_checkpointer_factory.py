"""R33-A1: env-gated checkpointer factory (spec P3 §4 LangGraph v1 depth).

R31-B added a ``MemorySaver`` checkpointer so approval-required runs
park at the HITL gate and resume after the decision.  But the
checkpointer was hard-wired to ``MemorySaver`` — there was no factory
that lets a production deployment swap in ``RedisSaver`` /
``PostgresSaver`` for cross-replica durability via an env flag.

This module pins the new factory contract:

  * ``build_checkpointer()`` reads ``CHECKPOINTER_BACKEND`` env:
      - ``memory`` (default) → :class:`MemorySaver`
      - ``redis``  → ``langgraph.checkpoint.redis.RedisSaver`` (if the
        ``langgraph-checkpoint-redis`` extra is installed)
      - ``postgres`` → ``langgraph.checkpoint.postgres.PostgresSaver``
        (if the ``langgraph-checkpoint-postgres`` extra is installed)
  * Missing optional deps degrade to ``MemorySaver`` with a warning —
    the runtime must stay resumable out of the box.
  * ``LangGraphRuntime._get_checkpointer`` calls the factory when no
    checkpointer was injected, so a plain ``LangGraphRuntime()`` honours
    the env.
  * Backward compat: with the env unset the factory returns a
    ``MemorySaver`` exactly as before.
"""

from __future__ import annotations

import pytest
from ai_employee.agent_platform_api.langgraph_runtime import (
    LangGraphRuntime,
    build_checkpointer,
)

# --------------------------------------------------------------------------- #
# 1. Default (env unset) → MemorySaver (backward compat)
# --------------------------------------------------------------------------- #


def test_default_backend_is_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``CHECKPOINTER_BACKEND`` unset the factory must return a
    ``MemorySaver`` — the pre-R33 default that every existing test
    assumes."""
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    cp = build_checkpointer()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


def test_explicit_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CHECKPOINTER_BACKEND=memory`` is an explicit spelling of the
    default and must also yield a ``MemorySaver``."""
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
    cp = build_checkpointer()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


# --------------------------------------------------------------------------- #
# 2. redis backend → RedisSaver when the dep is importable, else skip
# --------------------------------------------------------------------------- #


def _redis_saver_importable() -> bool:
    try:
        import langgraph.checkpoint.redis  # noqa: F401
    except Exception:
        return False
    return True


def test_redis_backend_returns_redis_saver_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CHECKPOINTER_BACKEND=redis`` must return a ``RedisSaver`` when
    the ``langgraph-checkpoint-redis`` extra is installed.  When the extra
    is missing the test skips — the factory itself degrades to
    ``MemorySaver`` (covered by the next test)."""
    if not _redis_saver_importable():
        pytest.skip("langgraph-checkpoint-redis not installed")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    # Provide a DSN so the saver's ``from_conn_string`` factory has what
    # it needs; construction is lazy (no connection is opened here).
    monkeypatch.setenv("REDIS_CHECKPOINT_URL", "redis://localhost:6379/0")
    cp = build_checkpointer()
    from langgraph.checkpoint.redis import RedisSaver

    assert isinstance(cp, RedisSaver)


def test_redis_backend_degrades_to_memory_when_dep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the redis extra is missing, ``CHECKPOINTER_BACKEND=redis``
    must *not* crash — it falls back to ``MemorySaver`` with a warning so
    the runtime stays resumable."""
    if _redis_saver_importable():
        pytest.skip("langgraph-checkpoint-redis is installed; degradation path not exercisable")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    cp = build_checkpointer()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


# --------------------------------------------------------------------------- #
# 3. postgres backend → PostgresSaver when the dep is importable, else skip
# --------------------------------------------------------------------------- #


def _postgres_saver_importable() -> bool:
    try:
        import langgraph.checkpoint.postgres  # noqa: F401
    except Exception:
        return False
    return True


def test_postgres_backend_returns_postgres_saver_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _postgres_saver_importable():
        pytest.skip("langgraph-checkpoint-postgres not installed")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_CHECKPOINT_URL", "postgresql://localhost:5432/lg")
    cp = build_checkpointer()
    from langgraph.checkpoint.postgres import PostgresSaver

    assert isinstance(cp, PostgresSaver)


def test_postgres_backend_degrades_to_memory_when_dep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if _postgres_saver_importable():
        pytest.skip("langgraph-checkpoint-postgres is installed; degradation path not exercisable")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    cp = build_checkpointer()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


# --------------------------------------------------------------------------- #
# 4. Unknown backend value → MemorySaver (defensive default)
# --------------------------------------------------------------------------- #


def test_unknown_backend_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised ``CHECKPOINTER_BACKEND`` value must degrade to
    ``MemorySaver`` rather than raising — misconfiguration must never
    make the runtime non-resumable."""
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "totally-bogus-backend")
    cp = build_checkpointer()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


# --------------------------------------------------------------------------- #
# 5. _get_checkpointer honours the env when no checkpointer injected
# --------------------------------------------------------------------------- #


def test_get_checkpointer_uses_factory_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime constructed with no explicit ``checkpointer`` must
    resolve its checkpointer through ``build_checkpointer()`` so the env
    flag is honoured."""
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
    runtime = LangGraphRuntime()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(runtime._get_checkpointer(), MemorySaver)


def test_get_checkpointer_injected_checkpointer_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a checkpointer is explicitly injected it must win over the
    env — tests (and cross-runtime durability callers) rely on the
    shared instance, not a freshly-built one."""
    from langgraph.checkpoint.memory import MemorySaver

    injected = MemorySaver()
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")  # would otherwise differ
    runtime = LangGraphRuntime(checkpointer=injected)
    assert runtime._get_checkpointer() is injected
