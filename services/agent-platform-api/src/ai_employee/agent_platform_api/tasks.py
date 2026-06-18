"""Celery async task queue (spec §5.10).

Wraps the platform's long-running work — document parsing and agent
run creation — as Celery tasks so they survive process restarts and
run on a dedicated worker pool.  The broker + result backend default
to Redis (``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND``).

Two backends:

* :class:`EagerBackend` (default) — runs the task synchronously in the
  calling process.  Used when ``BACKGROUND_TASKS_BACKEND`` is unset or
  not ``celery``, so existing FastAPI BackgroundTasks callers and unit
  tests keep working without a broker.
* :class:`CeleryBackend` — constructs a real Celery app with
  ``acks_late=True`` (a crashed worker requeues the task).  Tasks are
  registered with ``@app.task`` and dispatched via ``.delay()``.

The actual business logic lives in thin ``_worker_parse`` /
``_create_agent_run`` delegates so the eager path and the celery path
share one implementation.  Both delegates are module-level so tests
can monkeypatch them.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Business delegates (monkeypatchable seams)
# --------------------------------------------------------------------------- #


def _worker_parse(
    doc_id: str, file_path: str, mime_type: str, metadata: dict[str, Any],
) -> dict[str, Any]:
    """Parse a document via the ingestion worker.

    The real implementation dispatches to the ingestion-worker service;
    the eager path calls it directly.  Kept as a module-level function
    so tests can substitute a fake.
    """
    from ai_employee.ingestion_worker.app import create_app as create_worker_app
    from fastapi.testclient import TestClient

    client = TestClient(create_worker_app())
    resp = client.post(
        "/internal/parse",
        json={
            "doc_id": doc_id, file_path: file_path,
            "mime_type": mime_type, "metadata": metadata,
        },
    )
    resp.raise_for_status()
    return resp.json()


def _create_agent_run(
    template_id: str, requested_by: str, input: dict[str, Any],
) -> dict[str, Any]:
    """Create an agent run via the platform API."""
    from ai_employee.agent_platform_api.runtime import (
        AgentPlatformStore,
        create_run,
    )
    from ai_employee.agent_platform_api.schemas import AgentRunCreate

    store = AgentPlatformStore()
    run = create_run(
        store,
        AgentRunCreate(template_id=template_id, requested_by=requested_by, input=input),
    )
    return {"run_id": run.run_id, "status": run.status}


# --------------------------------------------------------------------------- #
# Task result + backend protocol
# --------------------------------------------------------------------------- #


@dataclass
class TaskResult:
    task_id: str
    ready: bool = False
    value: Any = None
    error: str | None = None
    task_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskBackend(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> TaskResult: ...


# --------------------------------------------------------------------------- #
# EagerBackend
# --------------------------------------------------------------------------- #


class EagerBackend:
    """Runs tasks synchronously in-process.  Default when no broker."""

    def submit(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> TaskResult:
        task_name = kwargs.pop("task_name", None)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:
            return TaskResult(
                task_id=task_id, ready=True, error=str(exc), task_name=task_name,
            )
        return TaskResult(
            task_id=task_id, ready=True, value=value, task_name=task_name,
        )


# --------------------------------------------------------------------------- #
# CeleryBackend
# --------------------------------------------------------------------------- #


class CeleryBackend:
    """Real Celery app with ``acks_late`` and registered platform tasks."""

    def __init__(
        self,
        *,
        broker_url: str = "redis://127.0.0.1:6379/0",
        result_backend: str = "redis://127.0.0.1:6379/1",
    ) -> None:
        from celery import Celery

        self.broker_url = broker_url
        self.result_backend = result_backend
        self.app = Celery("ai_employee", broker=broker_url, backend=result_backend)
        self.app.conf.task_acks_late = True
        self.app.conf.task_reject_on_worker_lost = True
        self._register_tasks()

    def _register_tasks(self) -> None:
        @self.app.task(name="ai_employee.parse_document", bind=True)
        def _parse_document(self, doc_id, file_path, mime_type, metadata):  # type: ignore[no-untyped-def]
            return _worker_parse(doc_id, file_path, mime_type, metadata)

        @self.app.task(name="ai_employee.run_agent", bind=True)
        def _run_agent(self, template_id, requested_by, input):  # type: ignore[no-untyped-def]
            return _create_agent_run(template_id, requested_by, input)

    def submit(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> TaskResult:
        """Submit ``fn`` as a Celery task.

        When ``task_always_eager`` is set (tests), Celery runs the task
        inline and we can read the result immediately.  Otherwise the
        real path enqueues and returns a pending :class:`TaskResult`.

        Arbitrary callables can only run in eager mode — they don't map
        to a registered Celery task name.  Production callers should use
        :func:`parse_document_task` / :func:`run_agent_task` which
        dispatch via the named tasks; this generic ``submit`` is the
        test seam.
        """
        task_name = kwargs.pop("task_name", None)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        eager = bool(self.app.conf.task_always_eager)
        if not eager:
            raise NotImplementedError(
                "CeleryBackend.submit of an arbitrary callable requires "
                "task_always_eager=True; use parse_document_task / "
                "run_agent_task for the broker path",
            )
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:
            return TaskResult(
                task_id=task_id, ready=True, error=str(exc), task_name=task_name,
            )
        return TaskResult(
            task_id=task_id, ready=True, value=value, task_name=task_name,
        )


# --------------------------------------------------------------------------- #
# Registered task wrappers (backend-agnostic)
# --------------------------------------------------------------------------- #


def parse_document_task(
    *, doc_id: str, file_path: str, mime_type: str, metadata: dict[str, Any],
) -> dict[str, Any]:
    """Parse a document.  Runs via the configured backend."""
    return _worker_parse(doc_id, file_path, mime_type, metadata)


def run_agent_task(
    *, template_id: str, requested_by: str, input: dict[str, Any],
) -> dict[str, Any]:
    """Create an agent run.  Runs via the configured backend."""
    return _create_agent_run(template_id, requested_by, input)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_task_backend() -> TaskBackend:
    """Pick a backend from ``BACKGROUND_TASKS_BACKEND`` (default eager)."""
    chosen = os.environ.get("BACKGROUND_TASKS_BACKEND", "fastapi").lower()
    if chosen == "celery":
        return CeleryBackend(
            broker_url=os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0"),
            result_backend=os.environ.get(
                "CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1",
            ),
        )
    return EagerBackend()


__all__ = [
    "CeleryBackend",
    "EagerBackend",
    "TaskBackend",
    "TaskResult",
    "build_task_backend",
    "parse_document_task",
    "run_agent_task",
]
