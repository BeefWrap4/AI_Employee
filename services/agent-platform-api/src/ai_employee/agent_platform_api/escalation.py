"""R20-3 approval escalation scheduler + notifier (spec §5.4).

A background-friendly sweep that escalates every pending approval task
older than the SLA threshold.  The threshold comes from
``APPROVAL_TIMEOUT_SECONDS`` (default 3600s = 1h).  Escalation marks
the task ``escalated``, routes it to an escalation reviewer, and
notifies that reviewer via a pluggable notifier.

The sweep is a pure function over the in-memory
:class:`AgentPlatformStore` so it can be driven by the existing
:class:`SchedulerLoop` (spec §5.8), a Celery beat task, or a k8s
CronJob — the call site decides the cadence.  The notifier is a
callable seam so tests can capture dispatches without a real message
bus.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ai_employee.agent_platform_api.runtime import (
    AgentPlatformStore,
    escalate_approval,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3600
"""Default SLA threshold for an open approval before it is escalated."""

Notifier = Callable[[dict[str, Any]], None]
"""A notifier seam: receives one escalation dispatch dict per call."""


def default_timeout_seconds() -> int:
    """Read the SLA threshold from ``APPROVAL_TIMEOUT_SECONDS``.

    Falls back to :data:`DEFAULT_TIMEOUT_SECONDS` (3600) when unset or
    unparsable.  Negative / zero values are clamped to the default so a
    misconfigured env cannot disable escalation silently.
    """
    raw = os.environ.get("APPROVAL_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "APPROVAL_TIMEOUT_SECONDS=%r is not an int; falling back to %s",
            raw, DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return value


def notify_escalation_reviewer(
    *,
    task_id: str,
    escalated_to: str,
    reason: str | None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """Dispatch an escalation notification to ``escalated_to``.

    The default notifier is a no-op logger; callers pass a capturing
    notifier in tests or a real message-bus publisher in production.
    Returns the dispatched payload so callers can log / audit it.
    """
    payload = {
        "task_id": task_id,
        "escalated_to": escalated_to,
        "reason": reason,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if notifier is None:
        logger.info(
            "approval.escalation.notify task_id=%s to=%s reason=%s",
            task_id, escalated_to, reason,
        )
    else:
        notifier(payload)
    return payload


def _task_age_seconds(task: Any) -> float | None:
    """Return the age of ``task`` in seconds, or None when unknown."""
    created = getattr(task, "created_at", None)
    if not created:
        return None
    if created.endswith("Z"):
        created = created.replace("Z", "+00:00")
    try:
        created_dt = datetime.fromisoformat(created)
    except ValueError:
        return None
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_dt).total_seconds()


def escalate_overdue_approvals(
    store: AgentPlatformStore,
    *,
    timeout_seconds: int | None = None,
    escalate_to: str | None = None,
    notifier: Notifier | None = None,
) -> list[str]:
    """Escalate every pending task older than the SLA threshold.

    Iterates over the store's approval tasks, escalates each pending
    task whose age exceeds ``timeout_seconds`` (default
    :func:`default_timeout_seconds`), and notifies the escalation
    reviewer.  Returns the list of escalated task ids (in store
    iteration order).  Idempotent: an already-escalated task is not
    re-escalated.
    """
    threshold = timeout_seconds if timeout_seconds is not None else default_timeout_seconds()
    escalated_ids: list[str] = []
    for task_id, task in list(store.approval_tasks.items()):
        if task.status != "pending":
            continue
        age = _task_age_seconds(task)
        if age is None or age < threshold:
            continue
        try:
            updated = escalate_approval(
                store,
                task_id=task_id,
                escalated_to=escalate_to,
                reason=f"SLA breach: pending > {threshold}s",
                escalated_by="system",
            )
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("escalate_overdue_approvals failed for %s", task_id)
            continue
        notify_escalation_reviewer(
            task_id=task_id,
            escalated_to=updated.escalated_to or escalate_to or "",
            reason=updated.escalation_reason,
            notifier=notifier,
        )
        escalated_ids.append(task_id)
    return escalated_ids


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "Notifier",
    "default_timeout_seconds",
    "escalate_overdue_approvals",
    "notify_escalation_reviewer",
]
