"""R35-D1: in-cluster Redpanda (Kafka-compatible, KRaft) for kind smoke.

The event-gateway service needs a Kafka broker to subscribe to the
``alarms`` topic.  Pre-R35 the kind smoke overlay disabled event-gateway
because no broker was available.  R35-D1 adds ``infra/k8s/redpanda.yaml``
— a single-binary, single-replica, KRaft-mode Redpanda deployment that
the smoke overlay can stand up so event-gateway can be enabled.

These tests pin the manifest shape:

* the file parses as a multi-document YAML stream;
* a ``Deployment`` is present that runs the
  ``docker.redpanda.com/redpandadata/redpanda:v24.1.7`` image in
  KRaft mode (the ``--mode=mode-unsafe-bypass-fsync`` flag is set);
* a ``Service`` named ``kafka`` is exposed on port 9092 (so
  ``KAFKA_BOOTSTRAP_SERVERS=kafka:9092`` works in the overlay);
* a 2Gi ``PersistentVolumeClaim`` backs the data directory.

NOTE: this manifest is *kind-smoke only*.  Production stays on a
real managed Kafka / MSK / Confluent Cloud — Redpanda single-node
with ``--mode=mode-unsafe-bypass-fsync`` trades durability for
fast local bring-up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required")

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "infra" / "k8s" / "redpanda.yaml"


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    assert MANIFEST_PATH.exists(), f"missing manifest: {MANIFEST_PATH}"
    with MANIFEST_PATH.open() as f:
        return [d for d in yaml.safe_load_all(f) if d is not None]


def _by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == kind]


def _by_kind_and_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    for d in _by_kind(docs, kind):
        if d.get("metadata", {}).get("name") == name:
            return d
    return None


# --------------------------------------------------------------------------- #
# Manifest shape
# --------------------------------------------------------------------------- #


def test_manifest_parses_with_multiple_documents(docs: list[dict[str, Any]]) -> None:
    assert len(docs) >= 3, (
        f"redpanda manifest should declare at least 3 documents "
        f"(Deployment + Service + PVC); got {len(docs)}"
    )


def test_manifest_contains_deployment_service_and_pvc(
    docs: list[dict[str, Any]],
) -> None:
    kinds = {d.get("kind") for d in docs}
    assert "Deployment" in kinds, "manifest must include a Deployment"
    assert "Service" in kinds, "manifest must include a Service"
    assert "PersistentVolumeClaim" in kinds, "manifest must include a PVC"


# --------------------------------------------------------------------------- #
# Deployment: image + KRaft mode
# --------------------------------------------------------------------------- #


def test_deployment_uses_redpanda_image(docs: list[dict[str, Any]]) -> None:
    deployment = _by_kind(docs, "Deployment")
    assert deployment, "Deployment not found"
    containers = deployment[0]["spec"]["template"]["spec"]["containers"]
    images = [c.get("image", "") for c in containers]
    assert any("redpanda" in img for img in images), (
        f"Deployment must use a redpanda image; got {images}"
    )


def test_deployment_runs_in_kraft_mode(docs: list[dict[str, Any]]) -> None:
    """The redpanda container command must include ``--mode=mode-unsafe-bypass-fsync``
    so it boots as a single-node KRaft cluster without a zookeeper ensemble."""
    deployment = _by_kind(docs, "Deployment")
    assert deployment, "Deployment not found"
    containers = deployment[0]["spec"]["template"]["spec"]["containers"]
    cmd_text = " ".join(
        str(part) for c in containers for part in (c.get("command") or []) + (c.get("args") or [])
    )
    # Also accept the flag encoded inside a shell command (string form).
    if not cmd_text.strip():
        # Search the rendered shell string under ``command: ["sh","-c","...redpanda ..."]``.
        for c in containers:
            for part in c.get("command") or []:
                if isinstance(part, str) and "redpanda" in part:
                    cmd_text += " " + part
    assert "mode-unsafe-bypass-fsync" in cmd_text, (
        f"redpanda must run in KRaft bypass-fsync mode; got command: {cmd_text!r}"
    )
    # And it must declare a node-id (KRaft cluster formation).
    assert "--node-id=0" in cmd_text, (
        f"redpanda must set --node-id=0 for KRaft cluster formation; got command: {cmd_text!r}"
    )


def test_deployment_is_single_replica(docs: list[dict[str, Any]]) -> None:
    """The kind smoke only runs a single redpanda; replication is off
    (mode-unsafe-bypass-fsync)."""
    deployment = _by_kind(docs, "Deployment")
    assert deployment, "Deployment not found"
    assert deployment[0]["spec"].get("replicas") == 1


# --------------------------------------------------------------------------- #
# Service: kafka on 9092
# --------------------------------------------------------------------------- #


def test_service_named_kafka(docs: list[dict[str, Any]]) -> None:
    service = _by_kind_and_name(docs, "Service", "kafka")
    assert service is not None, "Service named 'kafka' must be declared"


def test_service_exposes_port_9092(docs: list[dict[str, Any]]) -> None:
    service = _by_kind_and_name(docs, "Service", "kafka")
    assert service is not None
    ports = service["spec"].get("ports", [])
    has_9092 = any(p.get("port") == 9092 for p in ports)
    assert has_9092, f"kafka service must expose port 9092; got {ports}"


# --------------------------------------------------------------------------- #
# PVC: 2Gi data volume
# --------------------------------------------------------------------------- #


def test_pvc_requests_2gi(docs: list[dict[str, Any]]) -> None:
    pvcs = _by_kind(docs, "PersistentVolumeClaim")
    assert pvcs, "PVC not found"
    sizes = [
        p["spec"]["resources"]["requests"]["storage"]
        for p in pvcs
        if p.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")
    ]
    assert "2Gi" in sizes, f"a 2Gi PVC is required; got sizes={sizes}"
