"""Document upload progress tracker + SSE streaming (spec §4.4).

Tracks per-doc progress (bytes received, total, stage, error) and
publishes :class:`UploadProgress` events to subscribers.  The SSE
endpoint ``GET /api/v1/documents/{doc_id}/upload-progress`` replays the
last known progress snapshot then streams new events until the client
disconnects.

The default backend is process-local — restart wipes state, which is
fine for short-lived uploads.  Swap in a Redis-backed implementation
for HA deployments.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class UploadProgress:
    """Snapshot of an in-flight or completed upload."""

    doc_id: str
    stage: str = "unknown"  # unknown | receiving | parsing | completed | failed
    bytes_received: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    error: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UploadProgressTracker:
    """In-process tracker with one asyncio.Queue per subscriber."""

    def __init__(self) -> None:
        self._latest: dict[str, UploadProgress] = {}
        self._subscribers: dict[str, dict[str, asyncio.Queue[UploadProgress]]] = {}
        self._lock = asyncio.Lock()

    def reset_for_test(self) -> None:
        self._latest.clear()
        self._subscribers.clear()

    def _percent(self, received: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round(min(100.0, (received / total) * 100.0), 2)

    def update(
        self,
        *,
        doc_id: str,
        bytes_received: int,
        total_bytes: int,
        stage: str = "receiving",
    ) -> UploadProgress:
        progress = UploadProgress(
            doc_id=doc_id,
            stage=stage,
            bytes_received=bytes_received,
            total_bytes=total_bytes,
            percent=self._percent(bytes_received, total_bytes),
        )
        self._publish(doc_id, progress)
        return progress

    def complete(self, *, doc_id: str, total_bytes: int) -> UploadProgress:
        progress = UploadProgress(
            doc_id=doc_id,
            stage="completed",
            bytes_received=total_bytes,
            total_bytes=total_bytes,
            percent=100.0,
        )
        self._publish(doc_id, progress)
        return progress

    def fail(self, *, doc_id: str, error: str) -> UploadProgress:
        progress = UploadProgress(
            doc_id=doc_id,
            stage="failed",
            error=error,
        )
        self._publish(doc_id, progress)
        return progress

    def get(self, doc_id: str) -> UploadProgress:
        return self._latest.get(doc_id, UploadProgress(doc_id=doc_id))

    def subscribe(self, doc_id: str) -> asyncio.Queue[UploadProgress]:
        """Register a subscriber; returns the queue with the latest snapshot pre-loaded."""
        queue: asyncio.Queue[UploadProgress] = asyncio.Queue()
        queue_id = f"q_{id(queue)}"
        self._subscribers.setdefault(doc_id, {})[queue_id] = queue
        queue.put_nowait(self.get(doc_id))
        return queue

    def unsubscribe(self, *, doc_id: str, queue: asyncio.Queue[UploadProgress]) -> None:
        subs = self._subscribers.get(doc_id)
        if subs is None:
            return
        for qid, q in list(subs.items()):
            if q is queue:
                subs.pop(qid, None)
                break
        if not subs:
            self._subscribers.pop(doc_id, None)

    def _publish(self, doc_id: str, progress: UploadProgress) -> None:
        self._latest[doc_id] = progress
        for queue in list(self._subscribers.get(doc_id, {}).values()):
            try:
                queue.put_nowait(progress)
            except asyncio.QueueFull:
                pass


# --------------------------------------------------------------------------- #
# Module-level singleton + factory
# --------------------------------------------------------------------------- #

_tracker = UploadProgressTracker()


def build_progress_tracker() -> UploadProgressTracker:
    return _tracker


def record_upload_progress(
    *,
    doc_id: str,
    bytes_received: int,
    total_bytes: int,
    stage: str = "receiving",
) -> UploadProgress:
    """Convenience wrapper around the singleton tracker."""
    return _tracker.update(
        doc_id=doc_id,
        bytes_received=bytes_received,
        total_bytes=total_bytes,
        stage=stage,
    )


def complete_upload(*, doc_id: str, total_bytes: int) -> UploadProgress:
    return _tracker.complete(doc_id=doc_id, total_bytes=total_bytes)


def fail_upload(*, doc_id: str, error: str) -> UploadProgress:
    return _tracker.fail(doc_id=doc_id, error=error)


__all__ = [
    "UploadProgress",
    "UploadProgressTracker",
    "build_progress_tracker",
    "complete_upload",
    "fail_upload",
    "record_upload_progress",
]