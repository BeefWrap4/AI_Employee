"""R35-D2: enable event-gateway in the kind smoke overlay.

Pre-R35 the kind smoke overlay disabled event-gateway because the
cluster had no Kafka broker.  R35-D1 ships ``infra/k8s/redpanda.yaml``
so a real broker can be stood up in kind; this round flips
``infra/helm/values-smoke.yaml`` so a stock ``helm install`` (with
``-f values-smoke.yaml``) brings up event-gateway alongside the
redpanda broker.

These tests pin the smoke overlay shape:

* ``event-gateway.enabled`` is ``true`` (was ``false`` pre-R35);
* the overlay pins ``replicas: 1`` and ``storage: 0`` (event-gateway
  is stateless once the consumer state lives in the broker group
  offsets);
* ``KAFKA_BOOTSTRAP_SERVERS`` points at the in-cluster redpanda
  service ``kafka:9092`` (the same env var the production overlay
  uses, so the code path is identical);
* ``KAFKA_ALARM_TOPIC=alarms``, ``KAFKA_GROUP_ID=event-gateway`` and
  ``EVENT_GATEWAY_RCA_URL=http://rca-agent:8020`` are set so the
  consumer wires up end-to-end against the in-cluster rca-agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required")

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_VALUES_PATH = REPO_ROOT / "infra" / "helm" / "values-smoke.yaml"


@pytest.fixture(scope="module")
def smoke_values() -> dict[str, Any]:
    assert SMOKE_VALUES_PATH.exists(), f"missing overlay: {SMOKE_VALUES_PATH}"
    with SMOKE_VALUES_PATH.open() as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def event_gateway_block(smoke_values: dict[str, Any]) -> dict[str, Any]:
    eg = smoke_values.get("services", {}).get("event-gateway")
    assert eg is not None, "values-smoke.yaml must declare services.event-gateway"
    return eg


# --------------------------------------------------------------------------- #
# Cycle: overlay flips event-gateway on and points it at the in-cluster broker
# --------------------------------------------------------------------------- #


def test_event_gateway_enabled_in_smoke(event_gateway_block: dict[str, Any]) -> None:
    assert event_gateway_block.get("enabled") is True, (
        "services.event-gateway.enabled must be true in the smoke overlay "
        "(redpanda is now in-cluster; the broker is reachable at kafka:9092)"
    )


def test_event_gateway_replicas_pinned_to_one(
    event_gateway_block: dict[str, Any],
) -> None:
    """Single replica in kind smoke — the redpanda broker is single-node."""
    assert event_gateway_block.get("replicas") == 1, (
        f"event-gateway replicas must be 1 in smoke; got {event_gateway_block.get('replicas')}"
    )


def test_event_gateway_storage_is_zero(
    event_gateway_block: dict[str, Any],
) -> None:
    """Stateless consumer; offsets live in the broker group."""
    assert event_gateway_block.get("storage") in (0, "0"), (
        f"event-gateway storage must be 0 in smoke; got {event_gateway_block.get('storage')}"
    )


def test_kafka_bootstrap_servers_points_at_in_cluster_broker(
    event_gateway_block: dict[str, Any],
) -> None:
    env = event_gateway_block.get("env") or {}
    assert env.get("KAFKA_BOOTSTRAP_SERVERS") == "kafka:9092", (
        f"KAFKA_BOOTSTRAP_SERVERS must be kafka:9092 in smoke; "
        f"got {env.get('KAFKA_BOOTSTRAP_SERVERS')!r}"
    )


def test_kafka_alarm_topic_set(event_gateway_block: dict[str, Any]) -> None:
    env = event_gateway_block.get("env") or {}
    assert env.get("KAFKA_ALARM_TOPIC") == "alarms", (
        f"KAFKA_ALARM_TOPIC must be 'alarms' in smoke; got {env.get('KAFKA_ALARM_TOPIC')!r}"
    )


def test_kafka_group_id_set(event_gateway_block: dict[str, Any]) -> None:
    env = event_gateway_block.get("env") or {}
    assert env.get("KAFKA_GROUP_ID") == "event-gateway", (
        f"KAFKA_GROUP_ID must be 'event-gateway' in smoke; got {env.get('KAFKA_GROUP_ID')!r}"
    )


def test_event_gateway_rca_url_points_at_in_cluster_rca(
    event_gateway_block: dict[str, Any],
) -> None:
    env = event_gateway_block.get("env") or {}
    assert env.get("EVENT_GATEWAY_RCA_URL") == "http://rca-agent:8020", (
        f"EVENT_GATEWAY_RCA_URL must be http://rca-agent:8020 in smoke; "
        f"got {env.get('EVENT_GATEWAY_RCA_URL')!r}"
    )
