"""Celery async task queue tests (spec §5.10).

The Celery app is constructed with ``broker_url`` /
``result_backend`` from env (default Redis).  Tasks are registered with
``acks_late=True`` so a crash mid-task requeues the work.  When
``BACKGROUND_TASKS_BACKEND != 'celery'`` (the default), the factory
returns an in-process eager backend so existing FastAPI BackgroundTasks
tests keep passing without a broker.
"""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.tasks import (
    CeleryBackend,
    EagerBackend,
    TaskResult,
    build_task_backend,
    parse_document_task,
    run_agent_task,
)


# --------------------------------------------------------------------------- #
# EagerBackend (in-process, default)
# --------------------------------------------------------------------------- #


def test_eager_backend_runs_task_synchronously() -> None:
    backend = EagerBackend()

    def add(a, b):
        return a + b

    result = backend.submit(add, 2, 3)
    assert result.ready is True
    assert result.value == 5
    assert result.error is None


def test_eager_backend_captures_exception() -> None:
    backend = EagerBackend()

    def boom():
        raise ValueError("nope")

    result = backend.submit(boom)
    assert result.ready is True
    assert result.error is not None
    assert "nope" in result.error


def test_eager_backend_task_name_recorded() -> None:
    backend = EagerBackend()
    result = backend.submit(lambda: 42, task_name="compute_answer")
    assert result.task_name == "compute_answer"


# --------------------------------------------------------------------------- #
# TaskResult
# --------------------------------------------------------------------------- #


def test_task_result_to_dict() -> None:
    r = TaskResult(task_id="t1", ready=True, value=7, error=None, task_name="x")
    d = r.to_dict()
    assert d == {"task_id": "t1", "ready": True, "value": 7, "error": None, "task_name": "x"}


def test_task_result_not_ready() -> None:
    r = TaskResult(task_id="t1", ready=False)
    assert r.value is None
    assert r.error is None


# --------------------------------------------------------------------------- #
# build_task_backend
# --------------------------------------------------------------------------- #


def test_build_task_backend_default_is_eager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKGROUND_TASKS_BACKEND", raising=False)
    backend = build_task_backend()
    assert isinstance(backend, EagerBackend)


def test_build_task_backend_celery_returns_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKGROUND_TASKS_BACKEND", "celery")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
    backend = build_task_backend()
    assert isinstance(backend, CeleryBackend)
    assert backend.broker_url == "redis://127.0.0.1:6379/0"
    assert backend.app.conf.task_acks_late is True


def test_build_task_backend_unknown_falls_back_to_eager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKGROUND_TASKS_BACKEND", "kafka")
    backend = build_task_backend()
    assert isinstance(backend, EagerBackend)


# --------------------------------------------------------------------------- #
# Registered tasks (run against the eager backend)
# --------------------------------------------------------------------------- #


def test_parse_document_task_returns_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse_document_task delegates to the ingestion worker parser."""
    monkeypatch.delenv("BACKGROUND_TASKS_BACKEND", raising=False)
    backend = build_task_backend()

    captured = {}

    def fake_parse(doc_id, file_path, mime_type, metadata):
        captured["args"] = (doc_id, file_path, mime_type, metadata)
        return {"chunks": [{"chunk_id": "c1", "content": "alpha"}]}

    # Monkeypatch the worker function the task calls.
    import ai_employee.agent_platform_api.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "_worker_parse", fake_parse)
    result = backend.submit(
        parse_document_task,
        doc_id="d1", file_path="/tmp/x.md", mime_type="text/markdown", metadata={},
    )
    assert result.ready is True
    assert result.value["chunks"][0]["chunk_id"] == "c1"
    assert captured["args"][0] == "d1"


def test_run_agent_task_creates_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKGROUND_TASKS_BACKEND", raising=False)
    backend = build_task_backend()

    def fake_run(template_id, requested_by, input):
        return {"run_id": "agent_run_999", "status": "completed"}

    import ai_employee.agent_platform_api.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "_create_agent_run", fake_run)
    result = backend.submit(
        run_agent_task,
        template_id="knowledge_qa", requested_by="alice", input={"question": "q"},
    )
    assert result.ready is True
    assert result.value["run_id"] == "agent_run_999"


# --------------------------------------------------------------------------- #
# CeleryBackend (constructs the app without a live broker)
# --------------------------------------------------------------------------- #


def test_celery_backend_app_is_lazy_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = CeleryBackend(
        broker_url="memory://", result_backend="cache+memory://",
    )
    app = backend.app
    assert app.conf.broker_url == "memory://"
    assert app.conf.result_backend == "cache+memory://"
    assert app.conf.task_acks_late is True
    # Tasks are registered on the app.
    assert "ai_employee.parse_document" in app.tasks or any(
        t.endswith("parse_document") for t in app.tasks
    )


def test_celery_backend_submit_runs_eagerly_when_always_eager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CeleryBackend(broker_url="memory://", result_backend="cache+memory://")
    backend.app.conf.task_always_eager = True
    result = backend.submit(lambda x: x * 2, 21, task_name="double")
    assert result.ready is True
    assert result.value == 42
