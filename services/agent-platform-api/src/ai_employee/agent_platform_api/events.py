"""In-process event bus + WebSocket live-update plumbing (spec §5.5).

The :class:`EventBus` is a tiny pub/sub backed by :class:`asyncio.Queue`.
Process-local and lock-free; subscriptions are scoped to ``run_id`` so
a dashboard watching one run never sees noise from another.

The :class:`RunEvent` payload is JSON-serialisable; the WebSocket
endpoint in ``app.py`` forwards each event as a single text frame.

A module-level :data:`bus` singleton is exposed so producers (e.g.
``create_run`` or tool-call sites) can publish without taking a
dependency on the FastAPI app.

Multi-replica HA (R23-3): :class:`RedisEventBus` mirrors ``publish``
onto a Redis pub/sub channel; every replica runs a listener that fans
received messages back into its local bus so a WebSocket subscriber on
replica B receives an event published on replica A.  Activate with
``EVENT_BUS_BACKEND=redis`` (requires ``REDIS_URL``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RunEvent:
    """One streamed event in a run's lifecycle."""

    run_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunEvent:
        return cls(
            run_id=data["run_id"],
            event_type=data["event_type"],
            payload=data.get("payload", {}),
            ts=data.get("ts") or datetime.now(timezone.utc).isoformat(),
        )


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

    def _fan_out(self, event: RunEvent) -> None:
        """Push ``event`` to every current subscriber for its run_id."""
        for queue in list(self._subscribers.get(event.run_id, {}).values()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish(self, event: RunEvent) -> None:
        """Append ``event`` to history and push to all current subscribers."""
        history = self._history.setdefault(event.run_id, [])
        history.append(event)
        if len(history) > self._history_max:
            del history[: len(history) - self._history_max]
        self._fan_out(event)

    def history(self, run_id: str) -> list[RunEvent]:
        return list(self._history.get(run_id, []))


# --------------------------------------------------------------------------- #
# Module-level singleton + factory
# --------------------------------------------------------------------------- #

bus = EventBus()


def build_event_bus() -> EventBus:
    """Return the singleton :class:`EventBus`."""
    return bus


# --------------------------------------------------------------------------- #
# RedisEventBus (multi-replica)
# --------------------------------------------------------------------------- #


class RedisEventBus:
    """Wraps a local :class:`EventBus` and mirrors publish onto Redis.

    ``publish`` records history, fans out to local subscribers, AND
    publishes the event as JSON on a Redis channel.  A background
    listener (started via :meth:`start_listener`) subscribes to that
    channel and fans received events out to local subscribers — so a
    WebSocket client on replica B sees an event published on replica A.

    The listener path does NOT re-publish to Redis (the publisher
    already did) and does NOT double-record history, avoiding loops
    and duplicate replay.

    When Redis is unreachable, :meth:`publish` falls back to local-only
    delivery so a transient Redis outage degrades gracefully rather
    than dropping events on the floor.
    """

    def __init__(
        self,
        *,
        client: Any,
        channel: str = "ai_employee:events",
        local: EventBus | None = None,
    ) -> None:
        self._r = client
        self._channel = channel
        self._local = local or EventBus()
        self._listener_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started = threading.Event()

    # -- surface compatible with EventBus ------------------------------ #

    @property
    def local(self) -> EventBus:
        return self._local

    def reset_for_test(self) -> None:
        self._local.reset_for_test()

    def subscribe(self, run_id: str) -> tuple[str, asyncio.Queue[RunEvent]]:
        return self._local.subscribe(run_id)

    def unsubscribe(self, *, run_id: str, queue_id: str) -> None:
        self._local.unsubscribe(run_id=run_id, queue_id=queue_id)

    def history(self, run_id: str) -> list[RunEvent]:
        return self._local.history(run_id)

    def publish(self, event: RunEvent) -> None:
        # Local history + local fan-out first (so the publisher's own
        # subscribers see the event immediately), then mirror to Redis.
        self._local.publish(event)
        try:
            self._r.publish(self._channel, json.dumps(event.to_dict()))
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("redis event publish failed: %s", exc)

    # -- listener ------------------------------------------------------ #

    def start_listener(self) -> None:
        """Start the background pub/sub listener thread (idempotent)."""
        if self._listener_thread is not None and self._listener_thread.is_alive():
            return
        self._stop_event.clear()
        self._started.clear()
        self._listener_thread = threading.Thread(
            target=self._run,
            name="redis-event-bus",
            daemon=True,
        )
        self._listener_thread.start()
        # Give the listener a moment to subscribe before tests publish.
        self._started.wait(timeout=1.0)

    def stop_listener(self, *, timeout: float = 2.0) -> None:
        """Signal the listener to stop and wait for it to exit."""
        if self._listener_thread is None:
            return
        self._stop_event.set()
        try:
            self._listener_thread.join(timeout=timeout)
        except Exception:
            pass
        self._listener_thread = None

    def _run(self) -> None:
        self._started.set()
        try:
            pubsub = self._r.pubsub()
            pubsub.subscribe(self._channel)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("redis event bus subscribe failed: %s", exc)
            return
        handler = _RedisMessageHandler(self._local)
        pubsub.set_handler(handler.handle)
        try:
            while not self._stop_event.is_set():
                # Fake / real redis: poll get_message; sleep when idle.
                msg = pubsub.get_message(timeout=0.1)  # type: ignore[call-arg]
                if msg is not None and msg.get("type") == "message":
                    handler.handle(msg)
                else:
                    self._stop_event.wait(timeout=0.05)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass


class _RedisMessageHandler:
    """Decodes a Redis pub/sub message into a RunEvent and fans it out."""

    def __init__(self, local: EventBus) -> None:
        self._local = local

    def handle(self, msg: dict[str, Any]) -> None:
        data = msg.get("data")
        if data is None:
            return
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        try:
            event = RunEvent.from_dict(json.loads(data))
        except Exception as exc:  # pragma: no cover - bad payload
            logger.warning("redis event decode failed: %s", exc)
            return
        # Fan out to local subscribers only — do NOT re-publish to Redis
        # and do NOT re-record history (the publisher already did).
        self._local._fan_out(event)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_multi_replica_event_bus(
    *, redis_url: str | None = None, channel: str = "ai_employee:events"
) -> EventBus | RedisEventBus:
    """Return a RedisEventBus when REDIS_URL is set, else the local bus.

    When Redis is unreachable, falls back to the in-process singleton so
    single-replica dev/test keeps working.  The caller is responsible
    for calling ``start_listener()`` on the returned RedisEventBus.
    """
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    if not url:
        return bus
    try:
        import redis  # type: ignore[import-not-found]

        client = redis.Redis.from_url(
            url, socket_timeout=float(os.environ.get("REDIS_TIMEOUT_S", "0.5"))
        )
        client.ping()
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Redis unavailable for event bus: %s", exc)
        return bus
    return RedisEventBus(client=client, channel=channel, local=bus)


__all__ = [
    "EventBus",
    "RedisEventBus",
    "RunEvent",
    "build_event_bus",
    "build_multi_replica_event_bus",
    "bus",
]
