"""R35-A: ``/health`` must report the real backend / runtime, not a hardcoded label.

The three service ``/health`` endpoints used to lie:

- knowledge-api    hardcoded ``storage: "sqlite"`` (line 183 of app.py)
- agent-platform-api hardcoded ``runtime: "in_memory"`` (line 327 of app.py)
- rca-agent        hardcoded ``runtime: "in_memory_dag"`` (line 113 of app.py)

After R34 the services happily run against Postgres or LangGraph; the
health response has to reflect that so dashboards / on-call can see
the truth.  This test pins the corrected behaviour and is the single
source of truth for the contract change.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# knowledge-api: storage must mirror the actual build_knowledge_store return
# --------------------------------------------------------------------------- #


def test_knowledge_api_health_reports_sqlite_when_sqlite_store(
    knowledge_workspace,
) -> None:
    """Default (no DATABASE_URL) → build_knowledge_store returns
    SQLiteStore → /health must say storage: 'sqlite'."""
    from ai_employee.knowledge_api.app import create_app
    from ai_employee.knowledge_api.store import SQLiteStore
    from ai_employee.knowledge_api.worker_client import WorkerClient

    class _StubWorker(WorkerClient):
        def __init__(self) -> None:
            self._reachable = True

        def health(self) -> bool:
            return self._reachable

        def parse(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

    store = SQLiteStore(
        db_path=str(knowledge_workspace / "knowledge.sqlite3"),
        data_dir=str(knowledge_workspace),
    )
    store.init_schema()
    app = create_app(store=store, worker_client=_StubWorker())
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "knowledge-api"
    assert body["storage"] == "sqlite"
    print(f"\nR35-A knowledge-api (SQLite) /health = {body}")


def test_knowledge_api_health_reports_postgres_when_pg_store(
    knowledge_workspace, monkeypatch
) -> None:
    """When build_knowledge_store returns a PgKnowledgeStore instance,
    /health must report storage: 'postgres'."""
    from ai_employee.knowledge_api import app as kapp
    from ai_employee.knowledge_api.app import create_app
    from ai_employee.knowledge_api.worker_client import WorkerClient

    class _StubPgStore:
        """A minimal PgKnowledgeStore duck-type that exposes the class
        name ``PgKnowledgeStore`` so ``isinstance`` can detect it.
        """

        def init_schema(self) -> None:
            pass

    class _StubWorker(WorkerClient):
        def __init__(self) -> None:
            self._reachable = True

        def health(self) -> bool:
            return self._reachable

        def parse(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

    def fake_build(*args: Any, **kwargs: Any) -> Any:
        from ai_employee.knowledge_api.pg_store import PgKnowledgeStore

        # Use a sentinel PgKnowledgeStore subclass to avoid the
        # ``__init__``'s real ``db: DB`` typing — we only need
        # ``isinstance`` to be true for the /health branch.
        class _StubPg(PgKnowledgeStore):
            def __init__(self_inner) -> None:  # type: ignore[no-untyped-def]
                pass

            def init_schema(self_inner) -> None:
                pass

        return _StubPg()

    monkeypatch.setattr(kapp, "build_knowledge_store", fake_build)
    app = create_app(worker_client=_StubWorker())
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "knowledge-api"
    assert body["storage"] == "postgres"
    print(f"\nR35-A knowledge-api (PG) /health = {body}")


# --------------------------------------------------------------------------- #
# agent-platform-api: runtime must mirror RUNTIME_BACKEND
# --------------------------------------------------------------------------- #


def test_agent_platform_api_health_reports_dag_by_default(monkeypatch) -> None:
    """RUNTIME_BACKEND unset / 'dag' → /health must report runtime: 'dag'."""
    monkeypatch.delenv("RUNTIME_BACKEND", raising=False)
    from ai_employee.agent_platform_api.app import create_app

    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "agent-platform-api"
    assert body["runtime"] == "dag"
    print(f"\nR35-A agent-platform-api (dag) /health = {body}")


def test_agent_platform_api_health_reports_langgraph_when_env_set(
    monkeypatch,
) -> None:
    """RUNTIME_BACKEND=langgraph → /health must report runtime: 'langgraph'."""
    monkeypatch.setenv("RUNTIME_BACKEND", "langgraph")
    from ai_employee.agent_platform_api.app import create_app

    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "agent-platform-api"
    assert body["runtime"] == "langgraph"
    print(f"\nR35-A agent-platform-api (langgraph) /health = {body}")


# --------------------------------------------------------------------------- #
# rca-agent: runtime must reflect the actual RcaStore class
# --------------------------------------------------------------------------- #


def test_rca_agent_health_reports_in_memory_dag_for_bare_rca_store() -> None:
    """Default store (RcaStore() with no SQLite path) → in_memory_dag."""
    from ai_employee.rca_agent.app import create_app
    from ai_employee.rca_agent.runtime import RcaStore

    app = create_app(store=RcaStore())
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "rca-agent"
    assert body["runtime"] == "in_memory_dag"
    print(f"\nR35-A rca-agent (in_memory) /health = {body}")


def test_rca_agent_health_reports_sqlite_dag_for_sqlite_store(tmp_path) -> None:
    """SQLiteRcaStore → sqlite_dag."""
    from ai_employee.rca_agent.app import create_app
    from ai_employee.rca_agent.store import SQLiteRcaStore

    db_path = tmp_path / "rca.sqlite3"
    store = SQLiteRcaStore(str(db_path))
    app = create_app(store=store)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "rca-agent"
    assert body["runtime"] == "sqlite_dag"
    print(f"\nR35-A rca-agent (sqlite) /health = {body}")


def test_rca_agent_health_reports_postgres_dag_for_pg_store(tmp_path) -> None:
    """PgRcaStore → postgres_dag (so dashboards see the real backend)."""
    from ai_employee.rca_agent.app import create_app
    from ai_employee.rca_agent.pg_store import PgRcaStore

    class _StubPg(PgRcaStore):
        def __init__(self_inner) -> None:  # type: ignore[no-untyped-def]
            # Skip the real ``__init__`` (which needs a live ``DB``);
            # we only need ``isinstance(state, PgRcaStore)`` to be true
            # for the /health branch.
            pass

    store = _StubPg()
    app = create_app(store=store)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "rca-agent"
    assert body["runtime"] == "postgres_dag"
    print(f"\nR35-A rca-agent (postgres) /health = {body}")
