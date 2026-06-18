"""Cron tick loop that drives :class:`ScheduledRunStore` (spec §5.8).

Runs a background thread that periodically calls
:meth:`ScheduledRunStore.tick_due` and invokes the registered
``fire_callback`` for every due schedule.  Designed so the loop can
also be driven manually via :meth:`run_once` for tests.

The default behaviour is fail-soft: an exception inside the callback
is logged but does not stop the loop.  A misbehaving schedule cannot
take down the whole scheduler.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ai_employee.agent_platform_api.leader_election import (
    LeaderLease,
    LocalLeaderElection,
    build_leader_election,
)
from ai_employee.agent_platform_api.scheduled_runs import (
    ScheduledRun,
    ScheduledRunStore,
)

logger = logging.getLogger(__name__)

FireCallback = Callable[[ScheduledRun], Optional[str]]
"""A callback that handles one due schedule.

May return a ``run_id`` string to record it on the schedule via
:meth:`ScheduledRunStore.record_run` (when ``auto_record_runs=True``).
Returning ``None`` is fine; the loop just skips recording.
"""


class SchedulerLoop:
    """Background scheduler that ticks :class:`ScheduledRunStore`.

    When a :class:`LeaderLease` is configured (the default, via
    :func:`build_leader_election`), only the replica holding the lease
    ticks.  Non-leaders sleep through each interval and re-attempt
    acquisition — so a crashed leader is succeeded within one tick
    window.  The lease is renewed on every tick the leader performs.
    """

    def __init__(
        self,
        *,
        store: ScheduledRunStore,
        fire_callback: FireCallback,
        tick_interval_s: float = 30.0,
        auto_record_runs: bool = True,
        leader_lease: LeaderLease | None = None,
        renew_ratio: float = 0.5,
    ) -> None:
        self.store = store
        self.fire_callback = fire_callback
        self.tick_interval_s = max(0.05, float(tick_interval_s))
        self.auto_record_runs = auto_record_runs
        self.leader_lease: LeaderLease = leader_lease or LocalLeaderElection()
        # Renew the lease when this fraction of the interval has elapsed
        # since the last renewal, so we don't hammer Redis every tick.
        self._renew_every = max(1, int(1.0 / max(0.1, min(1.0, renew_ratio))))
        self._tick_count = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _ensure_leader(self) -> bool:
        """Acquire or renew the lease.  Returns True if we may tick."""
        if self.leader_lease.is_leader():
            self._tick_count += 1
            if self._tick_count % self._renew_every == 0:
                return self.leader_lease.renew()
            return True
        return self.leader_lease.try_acquire()

    def start(self) -> None:
        """Start the background tick thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started.clear()
        self._thread = threading.Thread(
            target=self._run, name="scheduler-loop", daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=1.0)

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the loop to stop and wait for the thread to exit."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("scheduler thread did not exit within timeout")
        self._thread = None
        # Release the lease so a standby can take over immediately.
        try:
            self.leader_lease.release()
        except Exception:  # noqa: BLE001
            pass

    def run_once(self) -> list[ScheduledRun]:
        """Tick the store once (if leader) and fire due schedules.

        Returns the list of schedules that were fired (empty when this
        replica is not the leader).  Exceptions from the callback are
        caught and logged so a bad schedule can't poison the batch.
        """
        if not self._ensure_leader():
            return []
        due = self.store.tick_due()
        for sched in due:
            try:
                result = self.fire_callback(sched)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "scheduler callback failed for %s: %s", sched.schedule_id, exc,
                )
                continue
            if self.auto_record_runs and isinstance(result, str) and result:
                self.store.record_run(schedule_id=sched.schedule_id, run_id=result)
        return due

    def _run(self) -> None:
        self._started.set()
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler tick failed: %s", exc)
            slept = 0.0
            while slept < self.tick_interval_s and not self._stop_event.is_set():
                time.sleep(min(0.1, self.tick_interval_s - slept))
                slept += 0.1


_loop: SchedulerLoop | None = None


def build_scheduler_loop() -> SchedulerLoop:
    """Return a process-wide singleton loop (lazy-initialised)."""
    global _loop
    if _loop is None:
        from ai_employee.agent_platform_api.scheduled_runs import (
            build_scheduled_run_store,
        )
        _loop = SchedulerLoop(
            store=build_scheduled_run_store(),
            fire_callback=lambda s: None,
            leader_lease=build_leader_election(),
        )
    return _loop


__all__ = [
    "FireCallback",
    "SchedulerLoop",
    "build_scheduler_loop",
]