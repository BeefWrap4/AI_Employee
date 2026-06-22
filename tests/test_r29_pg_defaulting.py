"""R29-A: PostgreSQL defaulting + runtime introspection.

These tests pin the user-facing contract for the PG-defaulting round:

1. Each ``build_*_store()`` factory must pick the PG backend when
   ``DATABASE_URL`` points at a Postgres URL and fall back to the
   SQLite/in-memory path when ``DATABASE_URL`` is unset (preserves
   dev/test behaviour).
2. When ``DATABASE_URL`` is unset, the factory must emit a one-shot
   deprecation warning so operators can see they're using the legacy
   default.  The warning is throttled to once per process.
3. Each ``create_app`` must log which backend it actually wired so an
   operator running ``kubectl logs`` can confirm PG vs SQLite at a
   glance without instrumenting the code.
4. The helm chart must inject ``DATABASE_URL`` into the four PG-backed
   services by default so production deploys land on PG without an
   extra values overlay.
5. The repo's ``.env.example`` must default ``DATABASE_URL`` to the
   local PG DSN so a fresh checkout is one ``docker compose up`` away
   from running against PG.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import logging as _logging  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 1) build_*_store() picks PG when DATABASE_URL is set
# --------------------------------------------------------------------------- #


def _isolate_pg_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set DATABASE_URL to a PG URL and clear DB-specific overrides.

    Returns the URL the test should expect to be honoured.  The
    :func:`open_db` call still tries to open a real psycopg
    connection; tests that don't need a real connection patch
    ``open_db`` themselves before invoking the factory.
    """
    url = "postgresql://ai_employee:ai_employee@localhost:5432/ai_employee"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("KNOWLEDGE_SQLITE_PATH", raising=False)
    monkeypatch.delenv("RCA_SQLITE_PATH", raising=False)
    return url


def test_build_knowledge_store_picks_pg_when_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DATABASE_URL is a PG URL, build_knowledge_store must return
    a PgKnowledgeStore (not SQLiteStore)."""
    _isolate_pg_url(monkeypatch)
    from ai_employee.knowledge_api.pg_store import PgKnowledgeStore, build_knowledge_store

    store = build_knowledge_store()
    assert isinstance(store, PgKnowledgeStore)


def test_build_rca_store_picks_pg_when_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DATABASE_URL is a PG URL, build_rca_store must return
    a PgRcaStore (not SQLiteRcaStore)."""
    _isolate_pg_url(monkeypatch)
    from ai_employee.rca_agent.pg_store import PgRcaStore, build_rca_store

    store = build_rca_store()
    assert isinstance(store, PgRcaStore)


def test_build_run_store_picks_pg_when_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DATABASE_URL is a PG URL, build_run_store must return
    a PgAgentRunStore (not AgentRunStore)."""
    _isolate_pg_url(monkeypatch)
    from ai_employee.agent_platform_api.pg_run_store import (
        PgAgentRunStore,
        build_run_store,
    )

    store = build_run_store()
    assert isinstance(store, PgAgentRunStore)


def test_build_approval_store_picks_pg_when_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DATABASE_URL is a PG URL, build_approval_store must return
    a PG-backed ApprovalTaskStore (db= attribute set)."""
    _isolate_pg_url(monkeypatch)
    from ai_employee.approval_service.store import ApprovalTaskStore, build_approval_store

    store = build_approval_store()
    # PG backend shares the SQLite store class but uses the unified DB
    # wrapper instead of a local sqlite3 file.  We assert on the
    # internal ``_db`` attribute (set in the PG branch).
    assert isinstance(store, ApprovalTaskStore)
    assert getattr(store, "_db", None) is not None, (
        "PG backend must set _db; SQLite path leaves _db=None and uses db_path"
    )


# --------------------------------------------------------------------------- #
# 2) build_*_store() falls back to SQLite when DATABASE_URL is unset
# --------------------------------------------------------------------------- #


def test_build_knowledge_store_picks_sqlite_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When DATABASE_URL is unset, build_knowledge_store must keep using
    SQLiteStore so dev/test (which never set DATABASE_URL) keeps working."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from ai_employee.knowledge_api.pg_store import build_knowledge_store
    from ai_employee.knowledge_api.store import SQLiteStore

    store = build_knowledge_store(
        db_path=str(tmp_path / "k.sqlite3"),
        data_dir=str(tmp_path),
    )
    assert isinstance(store, SQLiteStore)


def test_build_rca_store_picks_sqlite_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from ai_employee.rca_agent.pg_store import build_rca_store
    from ai_employee.rca_agent.store import SQLiteRcaStore

    store = build_rca_store(db_path=str(tmp_path / "rca.sqlite3"))
    assert isinstance(store, SQLiteRcaStore)


def test_build_run_store_picks_sqlite_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from ai_employee.agent_platform_api.pg_run_store import build_run_store
    from ai_employee.agent_platform_api.run_store import AgentRunStore

    store = build_run_store(db_path=str(tmp_path / "runs.sqlite3"))
    assert isinstance(store, AgentRunStore)
    # The default ctor opens a sqlite3 file at db_path.  We don't
    # assert on the path because AgentRunStore may lazy-init.


def test_build_approval_store_picks_sqlite_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from ai_employee.approval_service.store import ApprovalTaskStore, build_approval_store

    store = build_approval_store(db_path=str(tmp_path / "approval.sqlite3"))
    assert isinstance(store, ApprovalTaskStore)
    # SQLite path leaves _db=None and uses the on-disk db_path.
    assert getattr(store, "_db", None) is None


def _capture_logs(logger_name: str, level: int) -> tuple[list, logging.Handler]:
    """Attach a capture handler to ``logger_name`` and return (records, handler).

    We use a direct handler instead of ``caplog`` because some other
    test in the suite (``test_observability.test_logging_includes_trace_context``)
    calls ``configure_logging()`` which clears the root logger's
    handlers — including pytest's ``LogCaptureHandler``.  After that
    point ``caplog.records`` is silently empty for every subsequent
    test.  Attaching our own handler to the named logger keeps the
    assertion robust regardless of root-handler churn.

    The caller is responsible for ``logger.removeHandler(handler)``
    in a ``finally`` block.  We also force the named logger's level
    down to ``level`` so INFO records are not filtered out by a
    WARNING-level effective threshold left over from a previous test.
    """
    logger = logging.getLogger(logger_name)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    handler.setLevel(level)
    orig_level = logger.level
    logger.setLevel(level)
    # Some upstream test calls ``logging.disable(level)`` which can
    # suppress our records.  Re-enable by clearing the disable flag.
    orig_disable = logger.manager.disable
    logger.manager.disable = 0
    # Some upstream test also flips ``logger.disabled = True`` on
    # individual loggers (e.g. via ``logging.Logger.manager.disable``
    # or by direct assignment).  Clear that too so our handler
    # actually receives records.
    orig_disabled = logger.disabled
    logger.disabled = False
    logger.addHandler(handler)
    # Stash orig_level on the handler so the caller can restore it; we
    # do it in the helper's teardown instead to keep call sites short.
    handler._orig_logger_level = orig_level  # type: ignore[attr-defined]
    handler._orig_disable = orig_disable  # type: ignore[attr-defined]
    handler._orig_disabled = orig_disabled  # type: ignore[attr-defined]
    return records, handler


def _release_logs(logger_name: str, handler: logging.Handler) -> None:
    """Tear down a handler installed by :func:`_capture_logs`."""
    logger = logging.getLogger(logger_name)
    logger.removeHandler(handler)
    orig = getattr(handler, "_orig_logger_level", None)
    if orig is not None:
        logger.setLevel(orig)
    orig_disable = getattr(handler, "_orig_disable", None)
    if orig_disable is not None:
        logging.getLogger().manager.disable = orig_disable
    orig_disabled = getattr(handler, "_orig_disabled", None)
    if orig_disabled is not None:
        logger.disabled = orig_disabled


def test_build_knowledge_store_warns_once_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build_knowledge_store() must emit a deprecation warning the
    first time it's called with DATABASE_URL unset.  Subsequent calls
    must NOT emit another warning (one-shot throttle)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import ai_employee.knowledge_api.pg_store as pg_store_mod

    # Reset the throttle flag — conftest imports the app module which
    # calls build_knowledge_store() during collection, consuming the
    # one-shot warning.  We reset it here so this test exercises the
    # first-call behaviour cleanly.
    pg_store_mod._WARNED_FALLBACK = False
    from ai_employee.knowledge_api.pg_store import build_knowledge_store

    records, handler = _capture_logs("ai_employee.knowledge_api.pg_store", logging.WARNING)
    try:
        build_knowledge_store(
            db_path=str(tmp_path / "k1.sqlite3"),
            data_dir=str(tmp_path),
        )
        first = [r for r in records if "DATABASE_URL" in r.message]
        build_knowledge_store(
            db_path=str(tmp_path / "k2.sqlite3"),
            data_dir=str(tmp_path),
        )
        second = [r for r in records if "DATABASE_URL" in r.message]
    finally:
        _release_logs("ai_employee.knowledge_api.pg_store", handler)
    assert len(first) == 1, f"expected exactly 1 warning on first call, got {len(first)}"
    # second call should not re-emit (throttled).  ``second`` length
    # must equal ``first`` length — the second call adds 0 records.
    assert len(second) == 1, f"warning was re-emitted on second call (got {len(second)})"


def test_build_rca_store_warns_once_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import ai_employee.rca_agent.pg_store as pg_store_mod

    pg_store_mod._WARNED_FALLBACK = False
    from ai_employee.rca_agent.pg_store import build_rca_store

    records, handler = _capture_logs("ai_employee.rca_agent.pg_store", logging.WARNING)
    try:
        build_rca_store(db_path=str(tmp_path / "rca1.sqlite3"))
        first = [r for r in records if "DATABASE_URL" in r.message]
        build_rca_store(db_path=str(tmp_path / "rca2.sqlite3"))
        second = [r for r in records if "DATABASE_URL" in r.message]
    finally:
        _release_logs("ai_employee.rca_agent.pg_store", handler)
    assert len(first) == 1
    assert len(second) == 1


def test_build_run_store_warns_once_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import ai_employee.agent_platform_api.pg_run_store as pg_run_mod

    pg_run_mod._WARNED_FALLBACK = False
    from ai_employee.agent_platform_api.pg_run_store import build_run_store

    records, handler = _capture_logs("ai_employee.agent_platform_api.pg_run_store", logging.WARNING)
    try:
        build_run_store(db_path=str(tmp_path / "r1.sqlite3"))
        first = [r for r in records if "DATABASE_URL" in r.message]
        build_run_store(db_path=str(tmp_path / "r2.sqlite3"))
        second = [r for r in records if "DATABASE_URL" in r.message]
    finally:
        _release_logs("ai_employee.agent_platform_api.pg_run_store", handler)
    assert len(first) == 1
    assert len(second) == 1


def test_build_approval_store_warns_once_when_database_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import ai_employee.approval_service.store as store_mod

    store_mod._WARNED_FALLBACK = False
    from ai_employee.approval_service.store import build_approval_store

    records, handler = _capture_logs("ai_employee.approval_service.store", logging.WARNING)
    try:
        build_approval_store(db_path=str(tmp_path / "a1.sqlite3"))
        first = [r for r in records if "DATABASE_URL" in r.message]
        build_approval_store(db_path=str(tmp_path / "a2.sqlite3"))
        second = [r for r in records if "DATABASE_URL" in r.message]
    finally:
        _release_logs("ai_employee.approval_service.store", handler)
    assert len(first) == 1
    assert len(second) == 1


# --------------------------------------------------------------------------- #
# 4) create_app() logs which backend it wired at startup
# --------------------------------------------------------------------------- #


def test_knowledge_api_create_app_logs_sqlite_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With DATABASE_URL unset, knowledge-api create_app must emit a
    'using sqlite://...' startup log so operators can see the backend."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(tmp_path))

    from ai_employee.knowledge_api import app as app_mod

    records, handler = _capture_logs("ai_employee.knowledge_api.app", logging.INFO)
    try:
        app_mod.create_app()
    finally:
        _release_logs("ai_employee.knowledge_api.app", handler)
    backend_lines = [r.message for r in records if "using " in r.message]
    assert any("sqlite" in m for m in backend_lines), (
        f"expected 'using sqlite://...' log, got: {backend_lines}"
    )


def test_rca_agent_create_app_logs_sqlite_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """With DATABASE_URL unset, rca-agent create_app must emit a
    'using sqlite://...' startup log."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RCA_SQLITE_PATH", raising=False)

    from ai_employee.rca_agent import app as app_mod

    records, handler = _capture_logs("ai_employee.rca_agent.app", logging.INFO)
    try:
        app_mod.create_app()
    finally:
        _release_logs("ai_employee.rca_agent.app", handler)
    backend_lines = [r.message for r in records if "using " in r.message]
    assert any("sqlite" in m for m in backend_lines), (
        f"expected 'using sqlite://...' log, got: {backend_lines}"
    )


def test_agent_platform_create_app_logs_sqlite_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """With DATABASE_URL unset, agent-platform create_app must emit a
    'using sqlite://...' startup log."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from ai_employee.agent_platform_api import app as app_mod

    records, handler = _capture_logs("ai_employee.agent_platform_api.app", logging.INFO)
    try:
        app_mod.create_app()
    finally:
        _release_logs("ai_employee.agent_platform_api.app", handler)
    backend_lines = [r.message for r in records if "using " in r.message]
    assert any("sqlite" in m for m in backend_lines), (
        f"expected 'using sqlite://...' log, got: {backend_lines}"
    )


def test_approval_service_create_app_logs_sqlite_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With DATABASE_URL unset, approval-service create_app must emit a
    'using sqlite://...' startup log."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # approval-service create_app also depends on internal token / OIDC;
    # we just need the backend log to fire.

    from ai_employee.approval_service.app import create_app

    records, handler = _capture_logs("ai_employee.approval_service.app", logging.INFO)
    try:
        create_app()
    finally:
        _release_logs("ai_employee.approval_service.app", handler)
    backend_lines = [r.message for r in records if "using " in r.message]
    assert any("sqlite" in m for m in backend_lines), (
        f"expected 'using sqlite://...' log, got: {backend_lines}"
    )


def test_knowledge_api_create_app_logs_pg_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """With DATABASE_URL set to a PG URL, knowledge-api create_app must
    emit a 'using postgresql://...' startup log."""
    _isolate_pg_url(monkeypatch)

    from ai_employee.knowledge_api import app as app_mod

    records, handler = _capture_logs("ai_employee.knowledge_api.app", logging.INFO)
    try:
        app_mod.create_app()
    finally:
        _release_logs("ai_employee.knowledge_api.app", handler)
    backend_lines = [r.message for r in records if "using " in r.message]
    assert any("postgresql" in m for m in backend_lines), (
        f"expected 'using postgresql://...' log, got: {backend_lines}"
    )


# --------------------------------------------------------------------------- #
# 5) helm chart injects DATABASE_URL for the 4 PG-backed services
# --------------------------------------------------------------------------- #


def _has_helm() -> bool:
    import shutil

    return shutil.which("helm") is not None


def _render_helm_default() -> str:
    """Run ``helm template`` with the default values; return rendered YAML.

    Skip when the helm CLI is unavailable (the local Windows sandbox
    doesn't ship it; CI on Linux does).
    """
    if not _has_helm():
        pytest.skip("helm CLI not installed; skipping live template render")
    chart = REPO_ROOT / "infra" / "helm"
    result = subprocess.run(
        ["helm", "template", "ai-employee", str(chart)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_helm_default_renders_database_url_for_pg_services() -> None:
    """The default helm chart must inject DATABASE_URL into all 4
    PG-backed services (knowledge-api, rca-agent, agent-platform-api,
    approval-service) so a stock ``helm install`` lands on PG."""
    yaml = pytest.importorskip("yaml", reason="PyYAML required")
    rendered = _render_helm_default()
    docs = list(yaml.safe_load_all(rendered))
    deployments = [d for d in docs if d and d.get("kind") == "Deployment"]
    by_name = {d["metadata"]["name"]: d for d in deployments}

    for svc in (
        "knowledge-api",
        "rca-agent",
        "agent-platform-api",
        "approval-service",
    ):
        assert svc in by_name, f"missing Deployment for {svc}"
        env = by_name[svc]["spec"]["template"]["spec"]["containers"][0].get("env", [])
        db = next((e for e in env if e.get("name") == "DATABASE_URL"), None)
        assert db is not None, f"{svc} missing DATABASE_URL env var"
        val = db.get("value", "")
        assert val.startswith("postgresql://") or val.startswith("postgres://"), (
            f"{svc} DATABASE_URL={val!r} must be a PG URL"
        )
        # Must point at the cluster-local postgres Service (spec §5.4).
        assert "postgres" in val, f"{svc} DATABASE_URL={val!r} must reference 'postgres'"


def test_helm_database_url_default_is_overridable() -> None:
    """Operators must be able to override DATABASE_URL via --set without
    editing the chart.  Verifies the value is templated (not hardcoded)."""
    if not _has_helm():
        pytest.skip("helm CLI not installed; skipping live template render")
    yaml = pytest.importorskip("yaml", reason="PyYAML required")
    chart = REPO_ROOT / "infra" / "helm"
    result = subprocess.run(
        [
            "helm",
            "template",
            "ai-employee",
            str(chart),
            "--set",
            "global.databaseUrl=postgresql://u:p@custom-host:5432/db",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = list(yaml.safe_load_all(result.stdout))
    deployments = [d for d in docs if d and d.get("kind") == "Deployment"]
    by_name = {d["metadata"]["name"]: d for d in deployments}
    dep = by_name["knowledge-api"]
    env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
    db = next(e for e in env if e.get("name") == "DATABASE_URL")
    assert db["value"] == "postgresql://u:p@custom-host:5432/db"


# --------------------------------------------------------------------------- #
# 6) .env.example defaults DATABASE_URL to the local PG DSN
# --------------------------------------------------------------------------- #


def test_env_example_defaults_database_url_to_local_pg() -> None:
    """``DATABASE_URL=`` in .env.example must be set to a non-empty PG
    URL so a fresh checkout is one ``docker compose up`` away from PG."""
    env_file = REPO_ROOT / ".env.example"
    text = env_file.read_text(encoding="utf-8")
    # Find the first uncommented DATABASE_URL= line.
    match = re.search(
        r"^DATABASE_URL=(.+?)\s*$",
        text,
        flags=re.MULTILINE,
    )
    assert match is not None, "DATABASE_URL not found in .env.example"
    val = match.group(1).strip().strip('"').strip("'")
    assert val, "DATABASE_URL default in .env.example must be non-empty"
    assert val.startswith("postgres"), (
        f"expected DATABASE_URL default to be a postgres URL, got: {val!r}"
    )


def test_env_example_explains_sqlite_fallback() -> None:
    """``.env.example`` must include a comment that explains the
    unset → SQLite fallback so operators know how to opt out."""
    env_file = REPO_ROOT / ".env.example"
    text = env_file.read_text(encoding="utf-8")
    assert "SQLite" in text or "sqlite" in text, (
        ".env.example should mention SQLite fallback for unset DATABASE_URL"
    )
