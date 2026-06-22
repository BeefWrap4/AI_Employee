"""Alarm forwarder: Kafka messages → rca-agent HTTP POST.

The :class:`AlarmForwarder` is the glue between the
:class:`ai_employee.rca_agent.kafka_ingest.KafkaAlarmConsumer` (which
parses + converts raw Kafka payloads) and the rca-agent's existing
HTTP alarm endpoint.  The rca-agent stays unaware of Kafka.

The forwarder is also responsible for normalising the on-wire Kafka
schema (``alarm_id``/``site_id``/``alarm_code``/``severity``/``ts``)
to the rca-agent's :class:`RawAlarmEvent` schema (which adds
``alarm_name``/``vendor``/``ne_id``/``start_time``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class RcaClient(Protocol):
    """Protocol for the rca-agent HTTP shim used by the forwarder.

    Production wires :class:`HttpRcaClient` (httpx-based).  Tests
    inject a stub to avoid opening sockets.
    """

    def post_alarm(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpRcaClient:
    """``httpx``-backed implementation of :class:`RcaClient`.

    Posts the normalised :class:`RawAlarmEvent` JSON to
    ``<base_url>/api/v1/alarms/events`` with the cross-service
    internal token.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token or os.getenv("INTERNAL_TOKEN")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Internal-Token"] = self.token
        return h

    def post_alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
        # In production: real HTTP.  Tests monkeypatch this method.
        resp = httpx.post(  # pragma: no cover - real network path
            f"{self.base_url}/api/v1/alarms/events",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def _kafka_payload_to_raw_alarm(parsed: dict[str, Any]) -> dict[str, Any]:
    """Translate the on-wire Kafka alarm shape to :class:`RawAlarmEvent`.

    Both schemas share ``alarm_id``/``alarm_code``/``site_id``/
    ``severity``; the rca-agent additionally requires ``alarm_name``,
    ``vendor``, ``ne_id``, and ``start_time``.  Filled with sensible
    defaults when absent.
    """
    return {
        "alarm_id": parsed["alarm_id"],
        "alarm_code": parsed["alarm_code"],
        "alarm_name": parsed.get("alarm_name") or parsed["alarm_code"],
        "vendor": parsed.get("vendor", "unknown"),
        "site_id": parsed["site_id"],
        "cell_id": parsed.get("cell_id"),
        "ne_id": parsed.get("ne_id") or parsed["site_id"],
        "severity": parsed.get("severity", "major"),
        "start_time": parsed.get("ts") or parsed.get("start_time", ""),
        "clear_time": parsed.get("clear_time"),
        "raw_payload": parsed.get("raw")
        or {
            k: v
            for k, v in parsed.items()
            if k not in {"alarm_id", "site_id", "alarm_code", "severity", "ts"}
        },
    }


class AlarmForwarder:
    """Drains a :class:`KafkaAlarmConsumer` and forwards each alarm via HTTP.

    The class is intentionally side-effect-light so tests can inject
    both a :class:`FakeKafkaConsumer` (via the consumer parameter) and
    a stub :class:`RcaClient`.  Malformed Kafka messages are caught
    + logged so a single bad record cannot stall the partition.
    """

    def __init__(self, *, rca_client: RcaClient) -> None:
        self._rca = rca_client

    def drain_batch(
        self,
        *,
        kafka_consumer: Any,
        max_messages: int = 100,
    ) -> list[dict[str, Any]]:
        """Process one batch from the consumer, forwarding each to rca-agent.

        Mirrors :class:`KafkaAlarmConsumer.process_batch` but stops at
        the HTTP boundary: instead of writing to the rca-agent's
        ``RcaStore`` in-process, it POSTs the alarm to the rca-agent's
        HTTP endpoint and returns the rca-agent's JSON response list.
        """
        from ai_employee.rca_agent.kafka_ingest import parse_alarm_message

        raw_batch = kafka_consumer._consumer.poll(timeout_ms=500)  # type: ignore[attr-defined]
        if not raw_batch:
            return []
        forwarded: list[dict[str, Any]] = []
        for raw_msg in raw_batch[:max_messages]:
            try:
                payload = raw_msg.get("value", raw_msg)
                if isinstance(payload, (bytes, str)):
                    msg = parse_alarm_message(payload)
                else:
                    msg = parse_alarm_message(json.dumps(payload))
                raw_alarm = _kafka_payload_to_raw_alarm(
                    {
                        "alarm_id": msg.alarm_id,
                        "site_id": msg.site_id,
                        "alarm_code": msg.alarm_code,
                        "severity": msg.severity,
                        "ts": msg.ts,
                        **msg.raw,
                    }
                )
                resp = self._rca.post_alarm(raw_alarm)
                forwarded.append(resp)
            except Exception as exc:
                logger.warning("event-gateway dropping malformed alarm: %s", exc)
                continue
        if forwarded:
            kafka_consumer._consumer.commit()  # type: ignore[attr-defined]
        return forwarded


__all__ = ["AlarmForwarder", "HttpRcaClient", "RcaClient"]
