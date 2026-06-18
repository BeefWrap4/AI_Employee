"""WebSocket live updates tests (agent-platform-api).

The platform exposes ``/ws/runs/{run_id}`` so a dashboard can subscribe
to a run and receive streamed events without polling.  An in-process
:class:`EventBus` carries events from publishers (run lifecycle) to
subscribers; the WebSocket endpoint bridges the bus to the wire.
"""
from __future__ import annotations

import asyncio
import json

from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.events import (
    EventBus,
    RunEvent,
    build_event_bus,
    bus,
)
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# EventBus
# --------------------------------------------------------------------------- #


def test_event_bus_publish_delivers_to_subscriber() -> None:
    async def scenario() -> None:
        b = EventBus()
        qid, queue = b.subscribe("run_1")
        b.publish(RunEvent(
            run_id="run_1", event_type="run.step_changed",
            payload={"node": "ToolPlan"},
        ))
        ev = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev.event_type == "run.step_changed"
        b.unsubscribe(run_id="run_1", queue_id=qid)

    asyncio.run(scenario())


def test_event_bus_filters_by_run_id() -> None:
    async def scenario() -> None:
        b = EventBus()
        qid, queue = b.subscribe("run_1")
        b.publish(RunEvent(
            run_id="run_2", event_type="run.step_changed",
            payload={"node": "ToolPlan"},
        ))
        b.publish(RunEvent(
            run_id="run_1", event_type="run.step_changed",
            payload={"node": "ToolPlan"},
        ))
        ev = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev.run_id == "run_1"
        b.unsubscribe(run_id="run_1", queue_id=qid)

    asyncio.run(scenario())


def test_event_bus_replays_history_on_subscribe() -> None:
    b = EventBus()
    b.publish(RunEvent(
        run_id="run_1", event_type="run.started",
        payload={"requested_by": "alice"},
    ))
    qid, queue = b.subscribe("run_1")
    # The historical event should already be in the queue.
    ev = queue.get_nowait()
    assert ev.event_type == "run.started"
    assert ev.payload["requested_by"] == "alice"
    b.unsubscribe(run_id="run_1", queue_id=qid)


def test_event_bus_unsubscribe_removes_queue() -> None:
    b = EventBus()
    qid, _ = b.subscribe("run_1")
    assert "run_1" in b._subscribers  # internal but ok for assertions
    b.unsubscribe(run_id="run_1", queue_id=qid)
    assert "run_1" not in b._subscribers


def test_build_event_bus_returns_singleton() -> None:
    a = build_event_bus()
    b = build_event_bus()
    assert a is b


def test_run_event_serializes_to_dict() -> None:
    ev = RunEvent(
        run_id="run_1",
        event_type="approval.required",
        payload={"reviewer": "alice"},
        ts="2026-06-18T00:00:00Z",
    )
    payload = ev.to_dict()
    assert payload["run_id"] == "run_1"
    assert payload["event_type"] == "approval.required"
    assert payload["payload"]["reviewer"] == "alice"
    assert payload["ts"] == "2026-06-18T00:00:00Z"


# --------------------------------------------------------------------------- #
# WebSocket endpoint
# --------------------------------------------------------------------------- #


def test_websocket_replays_history_then_streams_new_events() -> None:
    bus.reset_for_test()
    bus.publish(RunEvent(
        run_id="run_x", event_type="run.step_changed",
        payload={"node": "TemplateLoaded"}, ts="2026-06-18T00:00:00Z",
    ))

    client = TestClient(create_app())
    with client.websocket_connect("/api/v1/ws/runs/run_x") as ws:
        first = json.loads(ws.receive_text())
        assert first["event_type"] == "run.step_changed"
        assert first["payload"]["node"] == "TemplateLoaded"

        bus.publish(RunEvent(
            run_id="run_x", event_type="tool_call.completed",
            payload={"tool": "knowledge-api.chat.query"},
        ))
        second = json.loads(ws.receive_text())
        assert second["event_type"] == "tool_call.completed"
        assert second["payload"]["tool"] == "knowledge-api.chat.query"
