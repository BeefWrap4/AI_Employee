"""R33-D: SSE run stream endpoint tests.

``GET /api/v1/agent-runs/{run_id}/stream`` returns a ``text/event-stream``
that replays the platform bus history (last 50 events) first, then streams
new events as ``data: {json}\\n\\n`` frames.  Each frame is a serialized
``RunEvent``.  Headers set ``Cache-Control: no-cache`` and
``X-Accel-Buffering: no`` so proxies don't buffer the stream.

The stream is infinite (it blocks on the event bus for new events), so
we drive the ASGI app directly with a ``receive`` callable that returns
the request body and then ``http.disconnect`` once we have collected the
frames we care about.  This exercises the real endpoint through the full
middleware stack (tenant middleware, exception handlers) and asserts on
the status code, headers, and SSE wire format — without the httpx
ASGI-transport deadlock that an infinite ``StreamingResponse`` triggers
under ``BaseHTTPMiddleware``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.events import RunEvent, bus

_FRAME_SEP = "\n\n"


def _parse_sse_frames(text: str) -> list[dict]:
    """Split an SSE text blob into ``data:`` payloads (JSON-decoded)."""
    frames: list[dict] = []
    for block in text.split(_FRAME_SEP):
        if not block.strip():
            continue
        for line in block.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
                break
    return frames


async def _collect_sse(
    app,
    run_id: str,
    *,
    n_frames: int,
    publish_after_open=None,
) -> tuple[int, dict[str, str], list[dict]]:
    """Drive ``app``'s SSE endpoint and collect ``n_frames`` SSE frames.

    Returns ``(status_code, headers, frames)``.  ``publish_after_open``
    is an optional coroutine that runs after the stream is open (so a
    test can publish live events while the stream is being read); it
    receives a callable ``request_disconnect`` that, when invoked,
    triggers the client to send ``http.disconnect``.
    """
    start_msg: dict = {}
    body_chunks: list[bytes] = []
    # ``state`` lets the receive callable and the publish task coordinate.
    state = {"request_sent": False, "disconnect": False}

    async def receive():
        if not state["request_sent"]:
            state["request_sent"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Spin until the test signals disconnect (after collecting enough
        # frames) or the publish task completes.  Yield control so the
        # streaming body iterator + publisher can make progress.
        while not state["disconnect"]:
            await asyncio.sleep(0.005)
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            start_msg.update(msg)
        elif msg["type"] == "http.response.body":
            body_chunks.append(msg.get("body", b""))
            text_so_far = b"".join(body_chunks).decode("utf-8", "replace")
            if len(_parse_sse_frames(text_so_far)) >= n_frames:
                state["disconnect"] = True

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/v1/agent-runs/{run_id}/stream",
        "raw_path": f"/api/v1/agent-runs/{run_id}/stream".encode(),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
        "app": app,
        "path_params": {},
    }

    publish_task = None
    if publish_after_open is not None:
        publish_task = asyncio.create_task(publish_after_open())
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=5.0)
    except asyncio.TimeoutError:
        pass
    finally:
        state["disconnect"] = True
        if publish_task is not None:
            publish_task.cancel()
            try:
                await publish_task
            except (asyncio.CancelledError, Exception):
                pass

    status = start_msg.get("status_code", 200)
    headers = {k.decode(): v.decode() for k, v in start_msg.get("headers", [])}
    text = b"".join(body_chunks).decode("utf-8", "replace")
    frames = _parse_sse_frames(text)
    return status, headers, frames


@pytest.fixture(autouse=True)
def _reset_bus():
    bus.reset_for_test()
    yield
    bus.reset_for_test()


# --------------------------------------------------------------------------- #
# (a) endpoint returns 200 + text/event-stream + headers
# --------------------------------------------------------------------------- #


def test_stream_returns_correct_content_type_and_headers() -> None:
    # Publish one historical event so the stream has a frame to emit
    # immediately — otherwise the response body never starts.
    bus.publish(
        RunEvent(
            run_id="run_hdr",
            event_type="run.started",
            payload={"requested_by": "alice"},
        )
    )
    app = create_app()

    async def scenario() -> None:
        status, headers, frames = await _collect_sse(app, "run_hdr", n_frames=1)
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"
        assert headers["x-accel-buffering"] == "no"
        assert len(frames) == 1
        assert frames[0]["event_type"] == "run.started"
        assert frames[0]["payload"]["requested_by"] == "alice"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (b) published bus events appear as SSE data frames in order
# --------------------------------------------------------------------------- #


def test_stream_delivers_new_events_in_order() -> None:
    run_id = "run_live"
    app = create_app()

    async def publish_after_open() -> None:
        # Wait for the stream to be subscribed before publishing.
        await asyncio.sleep(0.05)
        bus.publish(
            RunEvent(
                run_id=run_id,
                event_type="run.step_changed",
                payload={"node": "ToolPlan"},
            )
        )
        await asyncio.sleep(0.05)
        bus.publish(
            RunEvent(
                run_id=run_id,
                event_type="tool_call.completed",
                payload={"tool": "knowledge-api.chat.query"},
            )
        )

    async def scenario() -> None:
        status, _headers, frames = await _collect_sse(
            app, run_id, n_frames=2, publish_after_open=publish_after_open
        )
        assert status == 200
        assert [f["event_type"] for f in frames] == [
            "run.step_changed",
            "tool_call.completed",
        ]
        assert frames[0]["payload"]["node"] == "ToolPlan"
        assert frames[1]["payload"]["tool"] == "knowledge-api.chat.query"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (c) history replay: an event published BEFORE subscribe appears in stream
# --------------------------------------------------------------------------- #


def test_stream_replays_history_before_new_events() -> None:
    run_id = "run_hist"
    # Publish a historical event before the stream is opened.
    bus.publish(
        RunEvent(
            run_id=run_id,
            event_type="run.started",
            payload={"requested_by": "bob"},
            ts="2026-06-18T00:00:00Z",
        )
    )
    app = create_app()

    async def publish_new() -> None:
        await asyncio.sleep(0.05)
        bus.publish(
            RunEvent(
                run_id=run_id,
                event_type="run.completed",
                payload={"result": "ok"},
            )
        )

    async def scenario() -> None:
        status, _headers, frames = await _collect_sse(
            app, run_id, n_frames=2, publish_after_open=publish_new
        )
        assert status == 200
        # The historical event must come first, then the live one.
        assert [f["event_type"] for f in frames] == ["run.started", "run.completed"]
        assert frames[0]["payload"]["requested_by"] == "bob"
        assert frames[0]["ts"] == "2026-06-18T00:00:00Z"
        assert frames[1]["payload"]["result"] == "ok"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Bonus: raw generator contract — the underlying async generator emits the
# expected SSE wire format, independent of the HTTP transport.  Fast and
# fully deterministic (no background publish needed): history replay + a
# published live event, then break.
# --------------------------------------------------------------------------- #


def test_sse_frame_format_matches_run_event_serialization() -> None:
    from ai_employee.agent_platform_api.app import _sse_run_stream_generator

    bus.publish(
        RunEvent(
            run_id="run_fmt",
            event_type="run.started",
            payload={"requested_by": "carol"},
            ts="2026-06-18T00:00:00Z",
        )
    )

    async def scenario() -> str:
        gen = _sse_run_stream_generator("run_fmt")
        # Publish a live event after subscribe so it lands in the queue
        # after the replayed history.
        bus.publish(
            RunEvent(
                run_id="run_fmt",
                event_type="run.completed",
                payload={"result": "ok"},
                ts="2026-06-18T00:00:01Z",
            )
        )
        chunks: list[str] = []
        async for chunk in gen:
            chunks.append(chunk)
            if "run.completed" in chunk:
                break
        await gen.aclose()
        return "".join(chunks)

    text = asyncio.run(scenario())
    frames = _parse_sse_frames(text)
    assert len(frames) == 2
    assert frames[0]["event_type"] == "run.started"
    assert frames[1]["event_type"] == "run.completed"
    # Each frame is exactly ``data: {json}\n\n``.
    assert text.count("data: ") == 2
    assert text.count("\n\n") == 2


# --------------------------------------------------------------------------- #
# Disconnect cleanup: unsubscribe is called when the client disconnects.
# --------------------------------------------------------------------------- #


def test_stream_unsubscribes_on_disconnect() -> None:
    run_id = "run_cleanup"
    bus.publish(
        RunEvent(
            run_id=run_id,
            event_type="run.started",
            payload={},
        )
    )
    app = create_app()

    async def scenario() -> None:
        _, _, _ = await _collect_sse(app, run_id, n_frames=1)
        # After the client disconnects, the bus should have no
        # subscribers left for this run.
        assert run_id not in bus._subscribers

    asyncio.run(scenario())
