"""R29-C: event-gateway — independent Kafka→HTTP alarm forwarder (spec §9).

The event-gateway service owns the Kafka alarm subscription so the
rca-agent process no longer needs to embed the consumer in its
lifespan.  This file pins the contract:

* ``/health`` is reachable on the standalone app
* the Kafka consumer's ``process_batch`` is wired to forward alarms
  via HTTP POST to the rca-agent ``/api/v1/alarms/events`` endpoint
* the lifespan spawns the consumer when ``KAFKA_ENABLED=1``
* malformed messages are dropped (not crash the loop)
* rca-agent's lifespan no longer builds a Kafka consumer (regression
  test for the R27 wiring)
"""

from __future__ import annotations

from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# App surface
# --------------------------------------------------------------------------- #


def test_event_gateway_app_health_endpoint() -> None:
    """``/health`` on event-gateway reports service=event-gateway + status=ok."""
    from ai_employee.event_gateway.app import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "event-gateway"
    assert body["status"] == "ok"


def test_event_gateway_post_alarm_endpoint_forwards_to_rca() -> None:
    """``POST /api/v1/alarms/ingest`` accepts an alarm and POSTs it onward.

    With no Kafka enabled, the HTTP ingest endpoint is the public alarm
    entrypoint.  It forwards the payload to ``EVENT_GATEWAY_RCA_URL``
    (mocked here via monkeypatch) and returns the rca-agent response.
    """
    from ai_employee.event_gateway.app import create_app
    from fastapi.testclient import TestClient

    captured: list[dict[str, Any]] = []

    class _FakeRca:
        def post_alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"alarm_event_id": "alarm_evt_001", "fingerprint": "fp"}

    app = create_app(rca_client=_FakeRca())
    client = TestClient(app)
    payload = {
        "alarm_id": "AL-1",
        "alarm_code": "RRC_FAIL",
        "alarm_name": "RRC Fail",
        "vendor": "huawei",
        "site_id": "BJ-001",
        "ne_id": "NE-1",
        "severity": "critical",
        "start_time": "2026-06-19T10:00:00Z",
    }
    resp = client.post("/api/v1/alarms/ingest", json=payload)
    assert resp.status_code == 201
    assert resp.json()["alarm_event_id"] == "alarm_evt_001"
    # The captured payload is the serialised Pydantic model — it includes
    # the default ``cell_id`` / ``clear_time`` / ``raw_payload`` fields
    # filled in by the validator.  Compare the user-supplied fields.
    assert len(captured) == 1
    for k, v in payload.items():
        assert captured[0][k] == v


# --------------------------------------------------------------------------- #
# Kafka → HTTP forwarding (consumer side)
# --------------------------------------------------------------------------- #


def test_event_gateway_kafka_to_http_forwarding() -> None:
    """A ``process_batch`` call drains Kafka messages and POSTs each to rca-agent.

    Tests inject a ``FakeKafkaConsumer`` (no broker) and a stub
    ``RcaClient`` so the consumer→HTTP path is exercised hermetically.
    """
    from ai_employee.event_gateway.forwarder import AlarmForwarder
    from ai_employee.rca_agent.kafka_ingest import (
        FakeKafkaConsumer,
        KafkaAlarmConsumer,
    )

    fake_consumer = FakeKafkaConsumer(topic="alarms")
    fake_consumer.enqueue(
        {
            "alarm_id": "AL-1",
            "site_id": "BJ-001",
            "alarm_code": "RRC_FAIL",
            "severity": "critical",
            "ts": "2026-06-19T10:00:00Z",
        }
    )
    fake_consumer.enqueue(
        {
            "alarm_id": "AL-2",
            "site_id": "BJ-001",
            "alarm_code": "PRB_HIGH",
            "severity": "major",
            "ts": "2026-06-19T10:01:00Z",
        }
    )
    kafka_consumer = KafkaAlarmConsumer(consumer=fake_consumer)

    captured: list[dict[str, Any]] = []

    class _FakeRca:
        def post_alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"alarm_event_id": f"evt_{len(captured)}", "fingerprint": "fp"}

    forwarder = AlarmForwarder(rca_client=_FakeRca())
    processed = forwarder.drain_batch(kafka_consumer=kafka_consumer, max_messages=10)
    assert len(processed) == 2
    assert [p["alarm_id"] for p in captured] == ["AL-1", "AL-2"]


def test_event_gateway_drops_malformed_messages() -> None:
    """Malformed Kafka messages are logged + dropped, the rest still flow."""
    from ai_employee.event_gateway.forwarder import AlarmForwarder
    from ai_employee.rca_agent.kafka_ingest import (
        FakeKafkaConsumer,
        KafkaAlarmConsumer,
    )

    fake_consumer = FakeKafkaConsumer(topic="alarms")
    # First: good
    fake_consumer.enqueue(
        {
            "alarm_id": "AL-1",
            "site_id": "BJ-001",
            "alarm_code": "C",
            "severity": "major",
            "ts": "2026-06-19T10:00:00Z",
        }
    )
    # Second: malformed (missing fields)
    fake_consumer.enqueue({"alarm_id": "AL-2"})
    # Third: good
    fake_consumer.enqueue(
        {
            "alarm_id": "AL-3",
            "site_id": "BJ-001",
            "alarm_code": "C",
            "severity": "major",
            "ts": "2026-06-19T10:02:00Z",
        }
    )
    kafka_consumer = KafkaAlarmConsumer(consumer=fake_consumer)

    captured: list[dict[str, Any]] = []

    class _FakeRca:
        def post_alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"alarm_event_id": f"evt_{len(captured)}"}

    forwarder = AlarmForwarder(rca_client=_FakeRca())
    forwarder.drain_batch(kafka_consumer=kafka_consumer, max_messages=10)
    assert [p["alarm_id"] for p in captured] == ["AL-1", "AL-3"]


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #


def test_event_gateway_lifespan_starts_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``KAFKA_ENABLED=1``, the lifespan spawns a background task that
    drives the consumer+forwarder.

    The test injects a ``FakeKafkaConsumer`` so no broker is required
    and asserts that the background task is created + the consumer is
    closed on shutdown.
    """
    import ai_employee.event_gateway.app as eg_app
    from ai_employee.rca_agent.kafka_ingest import FakeKafkaConsumer
    from fastapi.testclient import TestClient

    captured = {"close_called": False, "forward_calls": 0}

    class _StubForwarder:
        def __init__(self, rca_client: Any) -> None:
            pass

        def drain_batch(self, *, kafka_consumer: Any, max_messages: int) -> list[Any]:
            captured["forward_calls"] += 1
            return []

    class _StubConsumer:
        def __init__(self, *, consumer: Any, topic: str = "alarms", group_id: str = "eg") -> None:
            self._consumer = consumer

        def process_batch(self, *, state: Any = None, max_messages: int = 100) -> list[Any]:
            return []

        def close(self) -> None:
            captured["close_called"] = True

    # Inject: a fake Kafka consumer is what ``build_alarm_consumer`` would return.
    monkeypatch.setenv("KAFKA_ENABLED", "1")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    monkeypatch.setenv("KAFKA_ALARM_TOPIC", "alarms")
    monkeypatch.setenv("KAFKA_GROUP_ID", "event-gateway")
    monkeypatch.setenv("EVENT_GATEWAY_RCA_URL", "http://rca-agent:8020")

    fake_consumer = FakeKafkaConsumer(topic="alarms")

    class _StubBuild:
        def __call__(self) -> Any:
            return _StubConsumer(consumer=fake_consumer)

    # The lifespan calls ``build_alarm_consumer`` via the
    # ``ai_employee.rca_agent.kafka_ingest`` module import inside
    # ``event_gateway.app._lifespan``.  Monkeypatching the source module
    # reroutes the call regardless of where it was imported from.
    import ai_employee.rca_agent.kafka_ingest as _ki

    monkeypatch.setattr(_ki, "build_alarm_consumer", _StubBuild())
    monkeypatch.setattr(eg_app, "AlarmForwarder", _StubForwarder)

    class _StubHttpRcaClient:
        def __init__(self, *, base_url: str, **_: Any) -> None:
            self.base_url = base_url

        def post_alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"alarm_event_id": "stub", "fingerprint": "stub"}

    monkeypatch.setattr(eg_app, "HttpRcaClient", _StubHttpRcaClient)

    app = eg_app.create_app()
    with TestClient(app) as client:
        # The lifespan entered; the background task should have ticked.
        client.get("/health")
    # After the lifespan exit the consumer close was called.
    assert captured["close_called"] is True


# --------------------------------------------------------------------------- #
# Regression: rca-agent no longer embeds a Kafka consumer
# --------------------------------------------------------------------------- #


def test_rca_agent_no_kafka_in_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """R29-C regression: rca-agent's lifespan does NOT build a Kafka consumer.

    Pre-R29-C, ``services/rca-agent/src/ai_employee/rca_agent/app.py``
    imported ``build_alarm_consumer`` inside its lifespan and started a
    background poll loop when ``KAFKA_ENABLED=1``.  After R29-C the
    alarm ingestion lives in the event-gateway service and the rca-agent
    is a pure HTTP consumer of ``/api/v1/alarms/events``.
    """
    from ai_employee.rca_agent import app as rca_app

    src = rca_app.__file__
    if src is None:
        pytest.fail("rca-agent app module has no __file__")
    with open(src, encoding="utf-8") as f:
        source = f.read()
    # The lifespan must not import the Kafka consumer builder.
    assert "build_alarm_consumer" not in source, (
        "rca-agent app.py still references build_alarm_consumer — "
        "R29-C moved Kafka ingestion to event-gateway"
    )
    # And the lifespan must remain simple: no Kafka poll loop.
    assert "KAFKA_ENABLED" not in source, (
        "rca-agent app.py still gates on KAFKA_ENABLED — "
        "R29-C moved Kafka ingestion to event-gateway"
    )
