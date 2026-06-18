"""Document upload progress streaming tests."""
from __future__ import annotations

import json

from ai_employee.knowledge_api.app import create_app
from ai_employee.knowledge_api.upload_progress import (
    UploadProgress,
    UploadProgressTracker,
    build_progress_tracker,
)
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# UploadProgressTracker
# --------------------------------------------------------------------------- #


def test_tracker_starts_unknown_for_new_doc() -> None:
    tracker = UploadProgressTracker()
    progress = tracker.get("doc_1")
    assert progress.stage == "unknown"
    assert progress.bytes_received == 0
    assert progress.total_bytes == 0
    assert progress.percent == 0.0


def test_tracker_records_progress() -> None:
    tracker = UploadProgressTracker()
    tracker.update(
        doc_id="doc_1",
        bytes_received=512,
        total_bytes=1024,
        stage="receiving",
    )
    progress = tracker.get("doc_1")
    assert progress.bytes_received == 512
    assert progress.total_bytes == 1024
    assert progress.percent == 50.0
    assert progress.stage == "receiving"


def test_tracker_marks_completion() -> None:
    tracker = UploadProgressTracker()
    tracker.complete(doc_id="doc_1", total_bytes=2048)
    progress = tracker.get("doc_1")
    assert progress.bytes_received == 2048
    assert progress.percent == 100.0
    assert progress.stage == "completed"


def test_tracker_marks_error() -> None:
    tracker = UploadProgressTracker()
    tracker.fail(doc_id="doc_1", error="connection lost")
    progress = tracker.get("doc_1")
    assert progress.stage == "failed"
    assert progress.error == "connection lost"


def test_tracker_subscribe_yields_events() -> None:
    """Subscribers receive a sequence of progress events for one doc_id."""
    import asyncio

    async def scenario() -> None:
        tracker = UploadProgressTracker()
        # Pre-seed so the subscribe() snapshot is non-trivial.
        tracker.update(doc_id="doc_1", bytes_received=100, total_bytes=200, stage="receiving")
        tracker.complete(doc_id="doc_1", total_bytes=200)
        queue = tracker.subscribe("doc_1")
        # Drain events: snapshot first, then nothing new unless we publish.
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        # Subscribe replays the latest snapshot.
        assert len(events) == 1
        assert events[0].stage == "completed"
        assert events[0].percent == 100.0

    asyncio.run(scenario())


def test_build_progress_tracker_returns_singleton() -> None:
    a = build_progress_tracker()
    b = build_progress_tracker()
    assert a is b


def test_upload_progress_to_dict_shape() -> None:
    p = UploadProgress(
        doc_id="d1", stage="receiving",
        bytes_received=100, total_bytes=200,
        percent=50.0, error=None,
        ts="2026-06-18T00:00:00Z",
    )
    d = p.to_dict()
    assert d["doc_id"] == "d1"
    assert d["percent"] == 50.0
    assert d["error"] is None


# --------------------------------------------------------------------------- #
# SSE endpoint
# --------------------------------------------------------------------------- #


def test_upload_progress_endpoint_returns_sse_for_known_doc() -> None:
    tracker = build_progress_tracker()
    tracker.reset_for_test()
    tracker.update(doc_id="doc_x", bytes_received=256, total_bytes=1024, stage="receiving")
    tracker.complete(doc_id="doc_x", total_bytes=1024)

    client = TestClient(create_app())
    with client.stream("GET", "/api/v1/documents/doc_x/upload-progress") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.read().decode("utf-8")
        frames = [line for line in body.split("\n") if line.startswith("data: ")]
        # The endpoint replays the latest snapshot then exits on completed.
        assert len(frames) == 1
        first = json.loads(frames[0][len("data: "):])
        assert first["doc_id"] == "doc_x"
        assert first["stage"] == "completed"
        assert first["percent"] == 100.0


def test_upload_progress_endpoint_for_unknown_doc_returns_empty_progress() -> None:
    build_progress_tracker().reset_for_test()
    client = TestClient(create_app())
    with client.stream("GET", "/api/v1/documents/never/upload-progress") as resp:
        assert resp.status_code == 200
        body = resp.read().decode("utf-8")
        # Even unknown docs emit a single synthetic "unknown" progress frame.
        frames = [line for line in body.split("\n") if line.startswith("data: ")]
        assert len(frames) >= 1
        first = json.loads(frames[0][len("data: "):])
        assert first["stage"] == "unknown"
