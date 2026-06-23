"""R33-A1 / R34-K: env-gated checkpointer factory (spec P3 §4 LangGraph v1 depth).

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

R34-K: now that ``langgraph-checkpoint-redis`` (0.4.1) and
``langgraph-checkpoint-postgres`` (3.1.0) are installed, the two
"available-path" tests RUN instead of skipping.  Both savers'
``from_conn_string`` are ``@contextmanager`` factories that return a
context manager (not the saver instance), and the Postgres one opens a
real connection on entry — so the tests monkeypatch
``from_conn_string`` with a stub context manager that yields a stub
saver of the right class.  This proves the factory routes the DSN to
the right saver WITHOUT needing a live broker.  The two
"degradation-path" tests inject a ``ModuleNotFoundError`` for the
relevant module via ``sys.modules`` so they still exercise the
graceful fallback even though the extras are now installed.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from typing import Any

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
# 2. redis backend → RedisSaver when the dep is importable
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
    """``CHECKPOINTER_BACKEND=redis`` must route the ``REDIS_CHECKPOINT_URL``
    DSN into ``RedisSaver.from_conn_string`` and return the yielded
    saver instance.

    R34-K: the deps are now installed, so this test RUNS.  The real
    ``from_conn_string`` is a ``@contextmanager`` whose entry constructs
    the saver (cheap — the Redis client is connection-pooled and only
    connects on the first command).  We monkeypatch it with a stub that
    yields a real ``RedisSaver`` constructed without a live Redis so the
    test never opens a socket — it only proves the factory routes the
    env DSN to the right saver class.
    """
    if not _redis_saver_importable():
        pytest.skip("langgraph-checkpoint-redis not installed")
    from langgraph.checkpoint.redis import RedisSaver

    captured: dict[str, Any] = {}

    @contextlib.contextmanager
    def _stub_from_conn_string(redis_url: str | None = None, **_: Any) -> Iterator[RedisSaver]:
        captured["redis_url"] = redis_url
        # Construct the saver directly — RedisSaver.__init__ only builds
        # the (lazy, pooled) client; no socket is opened here.
        yield RedisSaver(redis_url=redis_url)

    monkeypatch.setattr(RedisSaver, "from_conn_string", _stub_from_conn_string)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    dsn = "redis://localhost:6379/0"
    monkeypatch.setenv("REDIS_CHECKPOINT_URL", dsn)

    cp = build_checkpointer()

    assert isinstance(cp, RedisSaver)
    assert captured.get("redis_url") == dsn


def test_redis_backend_degrades_to_memory_when_dep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the redis extra is missing, ``CHECKPOINTER_BACKEND=redis``
    must *not* crash — it falls back to ``MemorySaver`` with a warning so
    the runtime stays resumable.

    R34-K: the extra is now installed, so we simulate its absence by
    injecting a ``ModuleNotFoundError`` into ``sys.modules`` for the
    ``langgraph.checkpoint.redis`` module.  This keeps the degradation
    path exercised in CI even though the dep is present.
    """
    _simulate_module_missing(monkeypatch, "langgraph.checkpoint.redis")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    cp = build_checkpointer()
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


# --------------------------------------------------------------------------- #
# 3. postgres backend → PostgresSaver when the dep is importable
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
    """``CHECKPOINTER_BACKEND=postgres`` must route the
    ``POSTGRES_CHECKPOINT_URL`` DSN into
    ``PostgresSaver.from_conn_string`` and return the yielded saver.

    R34-K: the real ``PostgresSaver.from_conn_string`` opens a live
    psycopg connection on context-manager entry — which would hang in CI
    without a broker.  We monkeypatch it with a stub that yields a
    ``PostgresSaver`` built from a dummy connection object so the test
    only proves the factory routes the env DSN to the right saver class
    (no socket is ever opened).
    """
    if not _postgres_saver_importable():
        pytest.skip("langgraph-checkpoint-postgres not installed")
    from langgraph.checkpoint.postgres import PostgresSaver

    captured: dict[str, Any] = {}

    @contextlib.contextmanager
    def _stub_from_conn_string(conn_string: str, **_: Any) -> Iterator[PostgresSaver]:
        captured["conn_string"] = conn_string
        # ``PostgresSaver.__init__`` requires a connection object; pass a
        # bare sentinel — the saver is never driven in this test, we only
        # assert the factory routed the DSN to the right class.
        yield PostgresSaver(conn=object())  # type: ignore[arg-type]

    monkeypatch.setattr(PostgresSaver, "from_conn_string", _stub_from_conn_string)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    dsn = "postgresql://localhost:5432/lg"
    monkeypatch.setenv("POSTGRES_CHECKPOINT_URL", dsn)

    cp = build_checkpointer()

    assert isinstance(cp, PostgresSaver)
    assert captured.get("conn_string") == dsn


def test_postgres_backend_degrades_to_memory_when_dep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the postgres extra is missing, ``CHECKPOINTER_BACKEND=postgres``
    must fall back to ``MemorySaver`` with a warning.

    R34-K: the extra is now installed, so we simulate its absence by
    injecting a ``ModuleNotFoundError`` into ``sys.modules`` for the
    ``langgraph.checkpoint.postgres`` module.
    """
    _simulate_module_missing(monkeypatch, "langgraph.checkpoint.postgres")
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _simulate_module_missing(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Make ``import <module_name>`` raise ``ModuleNotFoundError``.

    R34-K: the optional checkpoint extras are now installed, so the
    degradation-path tests can no longer rely on the import genuinely
    failing.  We simulate the missing-dep scenario by caching a
    ``ModuleNotFoundError`` raiser in ``sys.modules`` and removing any
    cached import of the module (and its parents' cached attrs) so the
    factory's ``from <module> import <Saver>`` triggers the fake failure.

    The monkeypatch is scoped to the test (teardown restores
    ``sys.modules``).
    """

    class _MissingModule:
        """Stand-in that raises ModuleNotFoundError on any attribute access."""

        def __getattr__(self, item: str) -> Any:
            raise ModuleNotFoundError(f"No module named '{module_name}' (simulated)")

    # Drop the module (and submodule caches) so the import machinery
    # re-resolves and picks up our injected sentinel.
    for key in list(sys.modules):
        if key == module_name or key.startswith(module_name + "."):
            monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.setitem(sys.modules, module_name, _MissingModule())
