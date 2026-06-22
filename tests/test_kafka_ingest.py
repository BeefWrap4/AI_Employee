"""Kafka alarm ingestion tests (spec P2/P3 §4 Kafka).

A :class:`KafkaAlarmConsumer` subscribes to the alarm topic and feeds
each message into the existing ``normalize_alarm`` pipeline.  The
consumer is pluggable: tests inject a :class:`FakeKafkaConsumer` so no
broker is required.  Production wires :mod:`aiokafka` (or confluent-
kafka) behind the same interface.
"""

from __future__ import annotations

import json

import pytest
from ai_employee.rca_agent.kafka_ingest import (
    AlarmMessage,
    FakeKafkaConsumer,
    KafkaAlarmConsumer,
    build_alarm_consumer,
    parse_alarm_message,
)

# --------------------------------------------------------------------------- #
# AlarmMessage + parser
# --------------------------------------------------------------------------- #


def test_parse_alarm_message_valid() -> None:
    raw = {
        "alarm_id": "AL-001",
        "site_id": "BJ-001",
        "alarm_code": "RRC_FAIL",
        "severity": "critical",
        "ts": "2026-06-18T10:00:00Z",
        "raw": {"ne": "gNB-01"},
    }
    msg = parse_alarm_message(json.dumps(raw))
    assert msg.alarm_id == "AL-001"
    assert msg.severity == "critical"
    assert msg.raw == {"ne": "gNB-01"}


def test_parse_alarm_message_missing_field_raises() -> None:
    raw = {"alarm_id": "AL-001"}  # missing site_id, alarm_code, severity, ts
    with pytest.raises(ValueError):
        parse_alarm_message(json.dumps(raw))


def test_parse_alarm_message_invalid_json_raises() -> None:
    with pytest.raises(ValueError):
        parse_alarm_message("not json")


def test_alarm_message_to_raw_alarm_event() -> None:
    """An AlarmMessage converts to the RawAlarmEvent the pipeline expects."""
    msg = AlarmMessage(
        alarm_id="AL-001",
        site_id="BJ-001",
        alarm_code="RRC_FAIL",
        severity="critical",
        ts="2026-06-18T10:00:00Z",
        raw={"ne": "gNB-01"},
    )
    raw = msg.to_raw_alarm_event()
    assert raw.alarm_id == "AL-001"
    assert raw.site_id == "BJ-001"


# --------------------------------------------------------------------------- #
# FakeKafkaConsumer
# --------------------------------------------------------------------------- #


def test_fake_consumer_poll_returns_queued_messages() -> None:
    consumer = FakeKafkaConsumer(topic="alarms")
    consumer.enqueue(
        {
            "alarm_id": "AL-1",
            "site_id": "S1",
            "alarm_code": "C",
            "severity": "major",
            "ts": "2026-06-18T10:00:00Z",
        }
    )
    batch = consumer.poll(timeout_ms=100)
    assert len(batch) == 1
    assert batch[0]["alarm_id"] == "AL-1"


def test_fake_consumer_poll_empty_returns_empty() -> None:
    consumer = FakeKafkaConsumer(topic="alarms")
    assert consumer.poll(timeout_ms=50) == []


def test_fake_consumer_commit_advances_offset() -> None:
    consumer = FakeKafkaConsumer(topic="alarms")
    consumer.enqueue(
        {
            "alarm_id": "AL-1",
            "site_id": "S1",
            "alarm_code": "C",
            "severity": "major",
            "ts": "2026-06-18T10:00:00Z",
        }
    )
    consumer.poll(timeout_ms=50)
    consumer.commit()
    # After commit, re-poll returns nothing (offset advanced).
    assert consumer.poll(timeout_ms=50) == []


# --------------------------------------------------------------------------- #
# KafkaAlarmConsumer — drains messages into normalize_alarm
# --------------------------------------------------------------------------- #


def _make_consumer(messages: list[dict]) -> KafkaAlarmConsumer:
    fake = FakeKafkaConsumer(topic="alarms")
    for m in messages:
        fake.enqueue(m)
    return KafkaAlarmConsumer(consumer=fake)  # type: ignore[arg-type]


def test_consumer_processes_one_batch_into_alarms() -> None:
    from ai_employee.rca_agent.app import create_app
    from ai_employee.rca_agent.runtime import RcaStore
    from fastapi.testclient import TestClient

    store = RcaStore()
    client = TestClient(create_app(store=store))
    # We need the in-process store the consumer writes to; use the app's.
    consumer = _make_consumer(
        [
            {
                "alarm_id": "AL-1",
                "site_id": "BJ-001",
                "alarm_code": "RRC_FAIL",
                "severity": "critical",
                "ts": "2026-06-18T10:00:00Z",
            },
            {
                "alarm_id": "AL-2",
                "site_id": "BJ-001",
                "alarm_code": "PRB_HIGH",
                "severity": "major",
                "ts": "2026-06-18T10:01:00Z",
            },
        ]
    )
    processed = consumer.process_batch(state=store, max_messages=10)
    assert len(processed) == 2
    assert processed[0].alarm_id == "AL-1"
    # Alarms were normalized into the store (keyed by alarm_event_id).
    assert any(a.alarm_id == "AL-1" for a in store.alarms.values())


def test_consumer_skips_malformed_message_continues() -> None:
    """A bad message is skipped (logged), the batch keeps going."""
    from ai_employee.rca_agent.runtime import RcaStore

    store = RcaStore()
    consumer = _make_consumer(
        [
            {
                "alarm_id": "AL-1",
                "site_id": "BJ-001",
                "alarm_code": "C",
                "severity": "major",
                "ts": "2026-06-18T10:00:00Z",
            },
            {"alarm_id": "AL-2"},  # malformed: missing fields
            {
                "alarm_id": "AL-3",
                "site_id": "BJ-001",
                "alarm_code": "C",
                "severity": "major",
                "ts": "2026-06-18T10:02:00Z",
            },
        ]
    )
    processed = consumer.process_batch(state=store, max_messages=10)
    assert len(processed) == 2  # AL-1 and AL-3; AL-2 skipped
    ids = {a.alarm_id for a in processed}
    assert ids == {"AL-1", "AL-3"}


def test_consumer_max_messages_caps_batch() -> None:
    from ai_employee.rca_agent.runtime import RcaStore

    store = RcaStore()
    msgs = [
        {
            "alarm_id": f"AL-{i}",
            "site_id": "S1",
            "alarm_code": "C",
            "severity": "major",
            "ts": "2026-06-18T10:00:00Z",
        }
        for i in range(5)
    ]
    consumer = _make_consumer(msgs)
    processed = consumer.process_batch(state=store, max_messages=2)
    assert len(processed) == 2


def test_consumer_empty_batch_returns_empty() -> None:
    from ai_employee.rca_agent.runtime import RcaStore

    store = RcaStore()
    consumer = _make_consumer([])
    assert consumer.process_batch(state=store, max_messages=10) == []


# --------------------------------------------------------------------------- #
# build_alarm_consumer
# --------------------------------------------------------------------------- #


def test_build_consumer_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAFKA_ENABLED", raising=False)
    assert build_alarm_consumer() is None


def test_build_consumer_enabled_returns_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAFKA_ENABLED", "1")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    monkeypatch.setenv("KAFKA_ALARM_TOPIC", "alarms")
    monkeypatch.setenv("KAFKA_GROUP_ID", "rca-agent")
    # Stub the kafka connect so no broker is needed.
    import ai_employee.rca_agent.kafka_ingest as ki

    monkeypatch.setattr(ki, "_connect_kafka", lambda **kw: FakeKafkaConsumer(topic="alarms"))
    consumer = build_alarm_consumer()
    assert consumer is not None
    assert consumer.topic == "alarms"
