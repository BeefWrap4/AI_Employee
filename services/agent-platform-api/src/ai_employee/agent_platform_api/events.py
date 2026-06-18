"""In-process event bus + WebSocket live-update plumbing (spec §5.5).

The :class:`EventBus` is a tiny pub/sub backed by :class:`asyncio.Queue`.
Process-local and lock-free; subscriptions are scoped to ``run_id`` so
a dashboard watching one run never sees noise from another.

The :class:`RunEvent` payload is JSON-serialisable; the WebSocket
endpoint in ``app.py`` forwards each event as a single text frame.

A module-level :data:`bus` singleton is exposed so producers (e.g.
``create_run`` or tool-call sites) can publish without taking a
dependency on the FastAPI app.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RunEvent:
    """One streamed event in a run's lifecycle."""

    run_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Async pub/sub scoped to ``run_id``.

    Subscribers receive events for one run at a time.  Backed by a
    dict of :class:`asyncio.Queue` instances — one queue per
    (run_id, queue_id) — so multiple subscribers can watch the same
    run independently.  When a queue's consumer stops iterating, the
    queue is removed from the registry so memory doesn't grow.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, dict[str, asyncio.Queue[RunEvent]]] = {}
        # History per run — last N events replayed on subscribe.
        self._history: dict[str, list[RunEvent]] = {}
        self._history_max = 50

    def reset_for_test(self) -> None:
        """Clear all subscribers and history (test helper)."""
        self._subscribers.clear()
        self._history.clear()

    def subscribe(self, run_id: str) -> tuple[str, asyncio.Queue[RunEvent]]:
        """Register a new subscriber; returns ``(queue_id, queue)``.

        The caller owns the queue: pop events with ``await queue.get()``
        and call :meth:`unsubscribe` when done.  Any backlog in history
        is enqueued first so the new subscriber sees recent context.
        """
        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        queue_id = f"q_{id(queue)}"
        self._subscribers.setdefault(run_id, {})[queue_id] = queue
        for ev in list(self._history.get(run_id, [])):
            queue.put_nowait(ev)
        return queue_id, queue

    def unsubscribe(self, *, run_id: str, queue_id: str) -> None:
        subs = self._subscribers.get(run_id)
        if subs is None:
            return
        subs.pop(queue_id, None)
        if not subs:
            self._subscribers.pop(run_id, None)

    def publish(self, event: RunEvent) -> None:
        """Append ``event`` to history and push to all current subscribers."""
        history = self._history.setdefault(event.run_id, [])
        history.append(event)
        if len(history) > self._history_max:
            del history[: len(history) - self._history_max]
        for queue in list(self._subscribers.get(event.run_id, {}).values()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def history(self, run_id: str) -> list[RunEvent]:
        return list(self._history.get(run_id, []))


# --------------------------------------------------------------------------- #
# Module-level singleton + factory
# --------------------------------------------------------------------------- #

bus = EventBus()


def build_event_bus() -> EventBus:
    """Return the singleton :class:`EventBus`."""
    return bus


__all__ = [
    "EventBus",
    "RunEvent",
    "build_event_bus",
    "bus",
]
