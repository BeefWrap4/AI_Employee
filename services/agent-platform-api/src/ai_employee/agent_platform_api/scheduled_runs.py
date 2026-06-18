"""Cron-triggered scheduled agent runs (spec §5.8).

A :class:`ScheduledRun` binds a template + input + cron expression to
a future execution time.  The :class:`ScheduledRunStore` is an
in-process registry that answers two questions:

* Which schedules are due *right now*?  (:meth:`tick_due`)
* When does each schedule fire next?  (recomputed after every tick)

The store is single-node; for multi-replica deployments the tick
should run from one leader with a distributed lock (e.g. Redis SETNX).
A lock is intentionally not implemented here — the platform today runs
as a single FastAPI process for the M0/M1 tier.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_employee.agent_platform_api.cron import (
    next_fire_after,
    parse_cron,
)


@dataclass
class ScheduledRun:
    schedule_id: str
    template_id: str
    cron: str
    input: dict[str, Any]
    requested_by: str
    next_fire_at: str
    last_fire_at: str | None = None
    fire_count: int = 0
    recent_run_ids: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class ScheduledRunStore:
    """Thread-safe in-memory registry of :class:`ScheduledRun`."""

    def __init__(self) -> None:
        self._schedules: dict[str, ScheduledRun] = {}
        self._count = 0
        self._lock = threading.Lock()

    def create(
        self,
        *,
        template_id: str,
        cron: str,
        input: dict[str, Any],
        requested_by: str,
    ) -> ScheduledRun:
        # Validate the cron string eagerly so bad input is rejected at
        # create-time, not at tick-time.
        expr = parse_cron(cron)
        nxt = next_fire_after(expr, datetime.now(timezone.utc))
        with self._lock:
            self._count += 1
            schedule_id = f"sched_{self._count:04d}_{uuid.uuid4().hex[:6]}"
            run = ScheduledRun(
                schedule_id=schedule_id,
                template_id=template_id,
                cron=cron,
                input=dict(input),
                requested_by=requested_by,
                next_fire_at=nxt.isoformat(),
            )
            self._schedules[schedule_id] = run
            return run

    def get(self, schedule_id: str) -> ScheduledRun | None:
        with self._lock:
            return self._schedules.get(schedule_id)

    def list_all(self) -> list[ScheduledRun]:
        with self._lock:
            return list(self._schedules.values())

    def delete(self, schedule_id: str) -> bool:
        with self._lock:
            return self._schedules.pop(schedule_id, None) is not None

    def tick_due(self, *, now: datetime | None = None) -> list[ScheduledRun]:
        """Return schedules whose ``next_fire_at`` <= now and advance them.

        For each due schedule, ``next_fire_at`` is recomputed for the
        next iteration and ``fire_count`` is incremented.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        due: list[ScheduledRun] = []
        with self._lock:
            for sched in list(self._schedules.values()):
                next_at = datetime.fromisoformat(sched.next_fire_at)
                if next_at.tzinfo is None:
                    next_at = next_at.replace(tzinfo=timezone.utc)
                if next_at <= now:
                    due.append(sched)
            for sched in due:
                expr = parse_cron(sched.cron)
                sched.last_fire_at = now.isoformat()
                sched.fire_count += 1
                nxt = next_fire_after(expr, now)
                sched.next_fire_at = nxt.isoformat()
        return due

    def record_run(self, *, schedule_id: str, run_id: str) -> None:
        """Append ``run_id`` to a schedule's ``recent_run_ids`` (capped)."""
        with self._lock:
            sched = self._schedules.get(schedule_id)
            if sched is None:
                return
            sched.recent_run_ids.append(run_id)
            if len(sched.recent_run_ids) > 50:
                sched.recent_run_ids = sched.recent_run_ids[-50:]


# --------------------------------------------------------------------------- #
# Module-level singleton + factory
# --------------------------------------------------------------------------- #

_store = ScheduledRunStore()


def build_scheduled_run_store() -> ScheduledRunStore:
    return _store


__all__ = [
    "ScheduledRun",
    "ScheduledRunStore",
    "build_scheduled_run_store",
]
