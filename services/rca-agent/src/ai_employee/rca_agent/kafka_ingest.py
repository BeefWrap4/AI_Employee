"""Kafka alarm ingestion (spec P2/P3 §4 Kafka).

The RCA agent consumes alarms from a Kafka topic instead of (or in
addition to) the synchronous ``POST /api/v1/alarms/events`` HTTP
endpoint.  Each consumed message is parsed into an :class:`AlarmMessage`,
converted to a :class:`RawAlarmEvent`, and fed into the existing
``normalize_alarm`` pipeline.

The Kafka client is pluggable behind :class:`KafkaConsumerProtocol`.
:func:`build_alarm_consumer` wires the real ``aiokafka`` consumer when
``KAFKA_ENABLED=1``; otherwise it returns ``None`` so services without
a broker keep working.  Tests inject :class:`FakeKafkaConsumer`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class AlarmMessage:
    """Parsed Kafka alarm message (the on-wire JSON shape)."""

    alarm_id: str
    site_id: str
    alarm_code: str
    severity: str = "major"
    ts: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_raw_alarm_event(self) -> Any:
        """Convert to the pipeline's :class:`RawAlarmEvent`.

        Fills sensible defaults for fields the Kafka message doesn't
        carry (alarm_name, vendor, ne_id, start_time) so a lean alarm
        payload still flows through normalization.
        """
        from ai_employee.rca_agent.schemas import RawAlarmEvent

        return RawAlarmEvent(
            alarm_id=self.alarm_id,
            alarm_code=self.alarm_code,
            alarm_name=self.raw.get("alarm_name", self.alarm_code),
            vendor=self.raw.get("vendor", "unknown"),
            site_id=self.site_id,
            cell_id=self.raw.get("cell_id"),
            ne_id=self.raw.get("ne_id", self.site_id),
            severity=self.severity,  # type: ignore[arg-type]
            start_time=self.ts or self.raw.get("start_time", ""),
            clear_time=self.raw.get("clear_time"),
            raw_payload=self.raw,
        )


def parse_alarm_message(raw: str | bytes) -> AlarmMessage:
    """Parse a Kafka message value into an :class:`AlarmMessage`.

    Raises ``ValueError`` on invalid JSON or missing required fields.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid alarm JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("alarm message must be a JSON object")
    required = ("alarm_id", "site_id", "alarm_code", "severity", "ts")
    missing = [f for f in required if f not in data]
    if missing:
        raise ValueError(f"alarm message missing fields: {missing}")
    return AlarmMessage(
        alarm_id=str(data["alarm_id"]),
        site_id=str(data["site_id"]),
        alarm_code=str(data["alarm_code"]),
        severity=str(data["severity"]),
        ts=str(data["ts"]),
        raw=data.get("raw") or {k: v for k, v in data.items() if k not in required},
    )


# --------------------------------------------------------------------------- #
# Consumer protocol + fake
# --------------------------------------------------------------------------- #


class KafkaConsumerProtocol(Protocol):
    def poll(self, *, timeout_ms: int) -> list[dict[str, Any]]: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


class FakeKafkaConsumer:
    """In-memory consumer for tests; enqueues raw message dicts."""

    def __init__(self, *, topic: str) -> None:
        self.topic = topic
        self._queue: list[dict[str, Any]] = []
        self._offset = 0

    def enqueue(self, message: dict[str, Any]) -> None:
        self._queue.append(message)

    def poll(self, *, timeout_ms: int) -> list[dict[str, Any]]:
        # Return any uncommitted messages.
        return list(self._queue[self._offset :])

    def commit(self) -> None:
        self._offset = len(self._queue)

    def close(self) -> None:
        self._queue.clear()
        self._offset = 0


# --------------------------------------------------------------------------- #
# KafkaAlarmConsumer
# --------------------------------------------------------------------------- #


class KafkaAlarmConsumer:
    """Drains alarm messages from Kafka into the RCA pipeline.

    ``process_batch`` polls up to ``max_messages``, parses each, and
    feeds it through ``normalize_alarm``.  Malformed messages are
    logged and skipped so one bad record can't stall the partition.
    """

    def __init__(
        self,
        *,
        consumer: KafkaConsumerProtocol,
        topic: str = "alarms",
        group_id: str = "rca-agent",
    ) -> None:
        self._consumer = consumer
        self.topic = topic
        self.group_id = group_id

    def process_batch(self, *, state: Any, max_messages: int = 100) -> list[Any]:
        """Poll + normalize up to ``max_messages`` alarms.

        Returns the list of normalized :class:`AlarmEvent` objects.
        Commits the offset after a successful batch.
        """
        from ai_employee.rca_agent.runtime import normalize_alarm

        raw_batch = self._consumer.poll(timeout_ms=500)
        if not raw_batch:
            return []
        processed: list[Any] = []
        for raw_msg in raw_batch[:max_messages]:
            try:
                payload = raw_msg.get("value", raw_msg)
                if isinstance(payload, (bytes, str)):
                    msg = parse_alarm_message(payload)
                else:
                    msg = parse_alarm_message(json.dumps(payload))
                raw_alarm = msg.to_raw_alarm_event()
                event = normalize_alarm(state, raw_alarm)
                processed.append(event)
            except Exception as exc:
                logger.warning("skipping malformed alarm message: %s", exc)
                continue
        if processed:
            self._consumer.commit()
        return processed

    def close(self) -> None:
        self._consumer.close()


class _SyncAdapter:
    """Bridge AIOKafkaConsumer to a sync poll/commit/close interface.

    aiokafka is async-only, so we run a dedicated background thread
    with its own event loop and drive ``getmany``/``commit`` from
    there.  ``poll()`` blocks up to ``timeout_ms`` then returns
    whatever the background thread has buffered; ``commit()`` is a
    fire-and-forget roundtrip; ``close()`` shuts the loop down.
    """

    _BATCH_QUEUE_MAX = 1000

    def __init__(self, async_consumer: Any) -> None:
        import queue as _queue
        import threading

        self._consumer = async_consumer
        self._queue: _queue.Queue = _queue.Queue(maxsize=self._BATCH_QUEUE_MAX)
        self._stop = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="aiokafka-poll",
            daemon=True,
        )
        self._thread.start()
        # Start the consumer (fire-and-forget).
        asyncio.run_coroutine_threadsafe(self._consumer.start(), self._loop)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            while not self._stop.is_set():
                fut = asyncio.run_coroutine_threadsafe(
                    self._consumer.getmany(timeout_ms=200, max_records=100),
                    self._loop,
                )
                try:
                    batches = fut.result(timeout=1.0)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("kafka getmany failed: %s", exc)
                    continue
                if not batches:
                    continue
                for _tp, msgs in batches.items():
                    for m in msgs:
                        raw = m.value
                        if isinstance(raw, (bytes, str)):
                            try:
                                raw = raw.decode() if isinstance(raw, bytes) else raw
                            except Exception:
                                continue
                        try:
                            self._queue.put_nowait({"value": raw, "offset": m.offset})
                        except Exception:
                            pass
        finally:
            self._loop.close()

    def poll(self, *, timeout_ms: int) -> list[dict[str, Any]]:
        import time as _time

        deadline = _time.monotonic() + (timeout_ms / 1000.0)
        out: list[dict[str, Any]] = []
        while _time.monotonic() < deadline and len(out) < 100:
            try:
                out.append(self._queue.get(timeout=0.05))
            except Exception:
                break
        return out

    def commit(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                self._consumer.commit(),
                self._loop,
            ).result(timeout=2.0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("kafka commit best-effort: %s", exc)

    def close(self) -> None:
        self._stop.set()
        try:
            self._thread.join(timeout=3.0)
        except Exception:
            pass


def _connect_kafka(
    *,
    bootstrap_servers: str,
    group_id: str,
    topic: str,
) -> KafkaConsumerProtocol:
    try:
        from aiokafka import AIOKafkaConsumer  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "aiokafka is required for KafkaAlarmConsumer; install with `pip install aiokafka`",
        ) from exc
    # AIOKafkaConsumer is async; wrap a thin sync adapter.  The
    # ``_SyncAdapter`` (defined at module level above) owns a dedicated
    # background thread that drives ``getmany`` synchronously, so the
    # rest of the consumer pipeline can stay purely synchronous.
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id=group_id,
        value_deserializer=lambda v: v,
    )
    return _SyncAdapter(consumer)


def build_alarm_consumer() -> KafkaAlarmConsumer | None:
    """Build a consumer from env.  Returns ``None`` when Kafka disabled.

    Env: ``KAFKA_ENABLED`` (truthy), ``KAFKA_BOOTSTRAP_SERVERS``,
    ``KAFKA_ALARM_TOPIC``, ``KAFKA_GROUP_ID``.
    """
    enabled = os.environ.get("KAFKA_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.environ.get("KAFKA_ALARM_TOPIC", "alarms")
    group_id = os.environ.get("KAFKA_GROUP_ID", "rca-agent")
    try:
        consumer = _connect_kafka(
            bootstrap_servers=bootstrap,
            group_id=group_id,
            topic=topic,
        )
    except Exception as exc:
        logger.warning("Kafka unavailable (%s): %s", bootstrap, exc)
        return None
    return KafkaAlarmConsumer(consumer=consumer, topic=topic, group_id=group_id)


__all__ = [
    "AlarmMessage",
    "FakeKafkaConsumer",
    "KafkaAlarmConsumer",
    "KafkaConsumerProtocol",
    "build_alarm_consumer",
    "parse_alarm_message",
]
