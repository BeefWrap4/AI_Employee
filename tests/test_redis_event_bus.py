"""RedisEventBus multi-replica tests (R23-3).

The in-process :class:`EventBus` only delivers events to subscribers
in the same process — a second agent-platform-api replica never sees a
run event published on the first.  :class:`RedisEventBus` wraps a
local :class:`EventBus` and mirrors ``publish`` onto a Redis pub/sub
channel; every replica runs a listener that fans received messages
back into its local bus, so a WebSocket subscriber on replica B
receives an event published on replica A.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from ai_employee.agent_platform_api.events import (
    RedisEventBus,
    RunEvent,
)


class _FakePubSub:
    """Minimal fake redis pub/sub shared between two bus instances.

    Tracks subscribers per channel and delivers messages synchronously
    to every registered handler.  Mirrors ``redis.Redis.pubsub()``.
    """

    def __init__(self, broker: _FakeRedisBroker) -> None:
        self._broker = broker
        self._handler = None

    def subscribe(self, channel: str) -> None:
        self._broker.subscribe(channel, self)

    def get_message(self, timeout: float | None = None):
        # Synchronous fake has no queued messages; delivery is via the
        # handler registered by set_handler.
        return None

    def set_handler(self, handler) -> None:
        self._handler = handler

    def deliver(self, channel: str, message: bytes) -> None:
        if self._handler is not None:
            self._handler({"type": "message", "channel": channel, "data": message})

    def close(self) -> None:
        self._broker.unsubscribe(self)


class _FakeRedisBroker:
    """Shared broker that two _FakeRedis clients publish/subscribe through."""

    def __init__(self) -> None:
        self._subs: dict[str, list[_FakePubSub]] = {}
        self._lock = threading.Lock()

    def subscribe(self, channel: str, ps: _FakePubSub) -> None:
        with self._lock:
            self._subs.setdefault(channel, []).append(ps)

    def unsubscribe(self, ps: _FakePubSub) -> None:
        with self._lock:
            for chans in self._subs.values():
                if ps in chans:
                    chans.remove(ps)

    def publish(self, channel: str, message: str) -> int:
        with self._lock:
            subs = list(self._subs.get(channel, []))
        data = message.encode("utf-8") if isinstance(message, str) else message
        for ps in subs:
            ps.deliver(channel, data)
        return len(subs)


class _FakeRedis:
    """Wraps a shared broker so two instances see each other's messages."""

    def __init__(self, broker: _FakeRedisBroker) -> None:
        self._broker = broker

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._broker)

    def publish(self, channel: str, message: str) -> int:
        return self._broker.publish(channel, message)


def test_redis_bus_delivers_locally() -> None:
    """publish on a RedisEventBus reaches a local subscriber."""
    broker = _FakeRedisBroker()
    bus = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus.start_listener()

    async def scenario() -> None:
        qid, queue = bus.subscribe("run_1")
        bus.publish(
            RunEvent(
                run_id="run_1",
                event_type="run.step_changed",
                payload={"node": "X"},
            )
        )
        ev = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev.event_type == "run.step_changed"
        bus.unsubscribe(run_id="run_1", queue_id=qid)

    try:
        asyncio.run(scenario())
    finally:
        bus.stop_listener()


def test_redis_bus_cross_replica_delivery() -> None:
    """A publish on bus A is delivered to a subscriber on bus B."""
    broker = _FakeRedisBroker()
    bus_a = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus_b = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus_a.start_listener()
    bus_b.start_listener()

    async def scenario() -> None:
        qid, queue = bus_b.subscribe("run_99")
        # Let the subscription propagate through the broker.
        await asyncio.sleep(0.01)
        bus_a.publish(
            RunEvent(
                run_id="run_99",
                event_type="tool_call.completed",
                payload={"t": "k"},
            )
        )
        ev = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev.run_id == "run_99"
        assert ev.event_type == "tool_call.completed"
        assert ev.payload["t"] == "k"
        bus_b.unsubscribe(run_id="run_99", queue_id=qid)

    try:
        asyncio.run(scenario())
    finally:
        bus_a.stop_listener()
        bus_b.stop_listener()


def test_redis_bus_filters_by_run_id() -> None:
    """A subscriber for run_1 does not receive events for run_2."""
    broker = _FakeRedisBroker()
    bus_a = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus_b = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus_a.start_listener()
    bus_b.start_listener()

    async def scenario() -> None:
        qid, queue = bus_b.subscribe("run_1")
        await asyncio.sleep(0.01)
        bus_a.publish(RunEvent(run_id="run_2", event_type="x", payload={}))
        bus_a.publish(RunEvent(run_id="run_1", event_type="y", payload={}))
        ev = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev.run_id == "run_1"
        bus_b.unsubscribe(run_id="run_1", queue_id=qid)

    try:
        asyncio.run(scenario())
    finally:
        bus_a.stop_listener()
        bus_b.stop_listener()


def test_redis_bus_history_replayed_on_subscribe() -> None:
    """Locally-published history is replayed to a new subscriber."""
    broker = _FakeRedisBroker()
    bus = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus.start_listener()
    try:
        bus.publish(RunEvent(run_id="run_1", event_type="run.started", payload={}))
        qid, queue = bus.subscribe("run_1")
        ev = queue.get_nowait()
        assert ev.event_type == "run.started"
        bus.unsubscribe(run_id="run_1", queue_id=qid)
    finally:
        bus.stop_listener()


def test_redis_bus_publish_serialises_event_as_json() -> None:
    """The wire payload is JSON with run_id / event_type / payload / ts."""
    broker = _FakeRedisBroker()
    bus = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    captured: list[str] = []

    real_publish = broker.publish

    def spy_publish(channel: str, message: str) -> int:
        captured.append(message)
        return real_publish(channel, message)

    broker.publish = spy_publish  # type: ignore[assignment]
    bus.start_listener()
    try:
        bus.publish(RunEvent(run_id="r", event_type="e", payload={"a": 1}))
        time.sleep(0.02)
        assert captured
        decoded = json.loads(captured[0])
        assert decoded["run_id"] == "r"
        assert decoded["event_type"] == "e"
        assert decoded["payload"] == {"a": 1}
    finally:
        bus.stop_listener()


def test_redis_bus_stop_listener_is_idempotent() -> None:
    broker = _FakeRedisBroker()
    bus = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    bus.start_listener()
    bus.stop_listener()
    bus.stop_listener()  # second stop must not raise


def test_redis_bus_redis_error_on_publish_does_not_raise() -> None:
    """When Redis itself errors, publish falls back to local-only delivery."""

    class BrokenRedis:
        def pubsub(self):
            raise ConnectionError("redis down")

        def publish(self, *a, **k):
            raise ConnectionError("redis down")

    bus = RedisEventBus(client=BrokenRedis(), channel="runs")
    # start_listener should not raise even though pubsub() fails.
    bus.start_listener()
    try:
        # publish must not raise and should still deliver locally.
        async def scenario() -> None:
            qid, queue = bus.subscribe("run_1")
            bus.publish(RunEvent(run_id="run_1", event_type="x", payload={}))
            ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev.event_type == "x"
            bus.unsubscribe(run_id="run_1", queue_id=qid)

        asyncio.run(scenario())
    finally:
        bus.stop_listener()


def test_redis_event_bus_implements_event_bus_protocol() -> None:
    """RedisEventBus exposes the same surface as the in-process EventBus."""
    broker = _FakeRedisBroker()
    bus = RedisEventBus(client=_FakeRedis(broker), channel="runs")
    for attr in ("subscribe", "unsubscribe", "publish", "history", "reset_for_test"):
        assert hasattr(bus, attr), f"missing {attr}"
    # Sanity: EventBus and RedisEventBus are interchangeable at the surface.
    assert callable(bus.subscribe) and callable(bus.publish)
