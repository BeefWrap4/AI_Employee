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

import pytest
from ai_employee.agent_platform_api.events import (
    RedisEventBus,
    RunEvent,
)


class _FakePubSub:
    """Minimal fake redis pub/sub shared between two bus instances.

    Mirrors ``redis.client.PubSub`` semantics: messages published to a
    channel the instance has ``subscribe``d to are queued, and
    ``get_message`` returns them one at a time (``None`` when the queue
    is empty).  There is no ``set_handler`` — redis-py 5.x doesn't have
    one — so delivery is purely via ``get_message`` polling, matching
    the real client the production code now targets.
    """

    def __init__(self, broker: _FakeRedisBroker) -> None:
        self._broker = broker
        self._channel: str | None = None
        self._queue: list[dict] = []
        self._lock = threading.Lock()

    def subscribe(self, channel: str) -> None:
        self._channel = channel
        self._broker.subscribe(channel, self)

    def get_message(self, timeout: float | None = None):
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        return None

    def deliver(self, channel: str, message: bytes) -> None:
        # Broker calls this on publish; queue the message for get_message.
        with self._lock:
            self._queue.append({"type": "message", "channel": channel, "data": message})

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


# --------------------------------------------------------------------------- #
# Real redis-py PubSub semantics (R28 fix).
#
# The _FakeRedis/_FakePubSub above are hand-rolled and silently accept methods
# that don't exist on the real redis-py PubSub (e.g. ``set_handler``).  These
# tests use ``fakeredis.FakeRedis`` which returns a genuine
# ``redis.client.PubSub`` instance, so they catch the
# AttributeError-kills-listener bug that the hand-rolled fake hides.
# --------------------------------------------------------------------------- #


def _real_redis_client():
    """Return a real redis-py PubSub-bearing client (fakeredis-backed)."""
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis()


def test_redis_bus_listener_thread_survives_with_real_pubsub() -> None:
    """The listener thread must not crash on a real redis-py PubSub.

    Regression: ``_run`` called ``pubsub.set_handler(...)`` which does not
    exist on ``redis.client.PubSub`` in redis-py 5.x — the AttributeError
    killed the listener thread immediately, so cross-replica delivery
    silently never happened.
    """
    import time

    bus = RedisEventBus(client=_real_redis_client(), channel="runs")
    bus.start_listener()
    try:
        # Give the thread a moment to either subscribe or crash.
        time.sleep(0.1)
        assert bus._listener_thread is not None
        assert bus._listener_thread.is_alive(), (
            "redis-event-bus listener thread died (real PubSub API mismatch)"
        )
    finally:
        bus.stop_listener()


def test_redis_bus_delivers_cross_replica_with_real_pubsub() -> None:
    """End-to-end cross-replica delivery using a real redis-py PubSub.

    Two buses share one fakeredis server (in-memory broker); a publish on
    bus A must reach a subscriber on bus B.  This only passes when the
    listener thread stays alive and correctly dispatches messages from
    ``get_message``.
    """

    fakeredis = pytest.importorskip("fakeredis")
    # Two clients against the SAME server share the pub/sub channel.
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeStrictRedis(server=server)
    client_b = fakeredis.FakeStrictRedis(server=server)

    bus_a = RedisEventBus(client=client_a, channel="runs")
    bus_b = RedisEventBus(client=client_b, channel="runs")
    bus_a.start_listener()
    bus_b.start_listener()
    try:

        async def scenario() -> None:
            qid, queue = bus_b.subscribe("run_real")
            # Let the subscription propagate through the broker.
            await asyncio.sleep(0.05)
            bus_a.publish(
                RunEvent(
                    run_id="run_real",
                    event_type="tool_call.completed",
                    payload={"t": "k"},
                )
            )
            ev = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert ev.run_id == "run_real"
            assert ev.event_type == "tool_call.completed"
            bus_b.unsubscribe(run_id="run_real", queue_id=qid)

        asyncio.run(scenario())
    finally:
        bus_a.stop_listener()
        bus_b.stop_listener()
