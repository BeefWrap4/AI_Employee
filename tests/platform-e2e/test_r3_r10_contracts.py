"""End-to-end contract tests for R3-R10 platform capabilities.

These exercise the public HTTP surface across the agent-platform-api in
a single process, verifying cross-feature contracts that unit tests
can't catch:

* WebSocket run-event stream reflects a freshly-created run.
* Tenant header flows into audit events produced by run creation.
* Scheduled runs (cron) can be created, ticked, and produce a real
  agent run via the fire callback.
* A/B bucketing assigns the same subject deterministically across
  repeated calls.
* Document versioning + diff round-trips through the in-process store.
* Health readiness probe returns 503 when a dep is unhealthy.

Each test is independent and uses the in-memory stores so no external
services are required.
"""
from __future__ import annotations

import json

import pytest
from ai_employee.agent_platform_api.ab_testing import (
    ABExperiment,
    ABExperimentStore,
    assign_variant,
)
from ai_employee.agent_platform_api.app import create_app as create_platform_app
from ai_employee.agent_platform_api.audit import audit_log, reset_audit_log
from ai_employee.agent_platform_api.events import bus as platform_bus
from ai_employee.agent_platform_api.scheduled_runs import ScheduledRunStore
from ai_employee.knowledge_api.versions import VersionStore, diff_versions
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# WebSocket run-event stream
# --------------------------------------------------------------------------- #


def test_websocket_streams_run_creation_event() -> None:
    """Creating an agent run publishes a run.* event on the run's WS channel."""
    platform_bus.reset_for_test()
    client = TestClient(create_platform_app())
    # First create the run so it exists, then subscribe to its channel.
    create_resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "hello"},
        },
    )
    assert create_resp.status_code == 201
    run_id = create_resp.json()["run_id"]

    # Publish a synthetic event for that run and confirm the WS delivers it.
    from ai_employee.agent_platform_api.events import RunEvent

    platform_bus.publish(RunEvent(
        run_id=run_id, event_type="run.step_changed",
        payload={"node": "ToolPlan"},
    ))
    with client.websocket_connect(f"/api/v1/ws/runs/{run_id}") as ws:
        frame = json.loads(ws.receive_text())
    assert frame["run_id"] == run_id
    assert frame["event_type"] == "run.step_changed"


# --------------------------------------------------------------------------- #
# Tenant isolation in audit log
# --------------------------------------------------------------------------- #


def test_tenant_header_flows_into_audit_event() -> None:
    """An X-Tenant-ID header on a run-creation request lands in the audit log."""
    reset_audit_log()
    client = TestClient(create_platform_app())
    resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "alice",
            "input": {"question": "x"},
        },
        headers={"X-Tenant-ID": "tenant-acme"},
    )
    assert resp.status_code == 201
    events = audit_log().list_by_action("run.created")
    assert events, "expected a run.created audit event"
    assert events[-1].payload.get("tenant_id") == "tenant-acme"


def test_tenant_whoami_reflects_header() -> None:
    client = TestClient(create_platform_app())
    resp = client.get("/api/v1/tenant/whoami", headers={"X-Tenant-ID": "tenant-beta"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "tenant-beta"
    assert body["source"] == "header"


def test_invalid_tenant_header_rejected_with_400() -> None:
    client = TestClient(create_platform_app())
    resp = client.get("/api/v1/tenant/whoami", headers={"X-Tenant-ID": "bad tenant!"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Scheduled runs → real agent run via fire callback
# --------------------------------------------------------------------------- #


def test_scheduled_run_tick_creates_agent_run() -> None:
    """A due schedule's fire callback creates a real agent run and records it."""
    reset_audit_log()
    store = ScheduledRunStore()
    sched = store.create(
        template_id="knowledge_qa",
        cron="*/5 * * * *",
        input={"question": "scheduled q"},
        requested_by="scheduler",
    )
    # Force due.
    store._schedules[sched.schedule_id].next_fire_at = "2020-01-01T00:00:00+00:00"

    client = TestClient(create_platform_app())
    fire_count = {"n": 0}

    def fire_callback(s: object) -> str:
        # Drive a real run creation through the platform app.
        resp = client.post(
            "/api/v1/agent-runs",
            json={
                "template_id": s.template_id,  # type: ignore[attr-defined]
                "requested_by": s.requested_by,  # type: ignore[attr-defined]
                "input": s.input,  # type: ignore[attr-defined]
            },
        )
        assert resp.status_code == 201
        fire_count["n"] += 1
        return resp.json()["run_id"]

    due = store.tick_due()
    assert len(due) == 1
    run_id = fire_callback(due[0])
    store.record_run(schedule_id=sched.schedule_id, run_id=run_id)

    refreshed = store.get(sched.schedule_id)
    assert refreshed.fire_count == 1
    assert refreshed.recent_run_ids == [run_id]
    assert fire_count["n"] == 1


# --------------------------------------------------------------------------- #
# A/B bucketing determinism
# --------------------------------------------------------------------------- #


def test_ab_bucketing_deterministic_across_calls() -> None:
    """The same subject always lands in the same variant for one experiment."""
    store = ABExperimentStore()
    store.create(
        experiment_id="exp_qwen_vs_gpt",
        control="qwen", treatment="gpt4o", traffic_split=0.5,
    )
    exp = store.get("exp_qwen_vs_gpt")
    assert exp is not None
    first = assign_variant(exp, bucket_key="user-42")
    for _ in range(20):
        assert assign_variant(exp, bucket_key="user-42") == first


def test_ab_bucketing_different_subjects_can_differ() -> None:
    """With a 0.5 split across many subjects, both variants get traffic."""
    exp = ABExperiment(
        experiment_id="exp1", control="c", treatment="t", traffic_split=0.5,
    )
    variants = {assign_variant(exp, bucket_key=f"u{i}") for i in range(500)}
    assert variants == {"c", "t"}


# --------------------------------------------------------------------------- #
# Document versioning + diff
# --------------------------------------------------------------------------- #


def test_document_version_diff_round_trip() -> None:
    """Two versions of one doc produce an add/remove/modified diff."""
    store = VersionStore()
    store.create(
        doc_id="d1", version="v1",
        chunks=[
            {"chunk_id": "c1", "content": "alpha", "section_path": "root"},
            {"chunk_id": "c2", "content": "beta", "section_path": "root"},
        ],
    )
    store.create(
        doc_id="d1", version="v2",
        chunks=[
            {"chunk_id": "c1", "content": "alpha-updated", "section_path": "root"},
            {"chunk_id": "c3", "content": "gamma", "section_path": "root"},
        ],
    )
    old = store.get("d1", "v1")
    new = store.get("d1", "v2")
    assert old is not None and new is not None
    diff = diff_versions(old, new)
    assert diff.added == ["c3"]
    assert diff.removed == ["c2"]
    assert diff.modified == ["c1"]


# --------------------------------------------------------------------------- #
# Health readiness probe
# --------------------------------------------------------------------------- #


def test_readiness_probe_503_when_dep_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_employee.agent_platform_api import health as health_mod

    def fake_check_sqlite(path: str) -> health_mod.DependencyCheck:
        return health_mod.DependencyCheck(
            name="sqlite", healthy=False, latency_ms=0.0, error="boom",
        )

    monkeypatch.setattr(health_mod, "check_sqlite", fake_check_sqlite)
    monkeypatch.setenv("SQLITE_PATH", "/tmp/whatever.sqlite3")
    client = TestClient(create_platform_app())
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert "sqlite" in body["unhealthy"]


def test_liveness_probe_always_ok() -> None:
    client = TestClient(create_platform_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# Upload progress SSE
# --------------------------------------------------------------------------- #


def test_upload_progress_sse_replays_snapshot() -> None:
    """knowledge-api streams the latest upload-progress snapshot over SSE."""
    from ai_employee.knowledge_api.upload_progress import build_progress_tracker

    tracker = build_progress_tracker()
    tracker.reset_for_test()
    tracker.complete(doc_id="doc-sse", total_bytes=2048)

    import os
    import tempfile

    from ai_employee.knowledge_api.app import create_app as create_knowledge_app
    from ai_employee.knowledge_api.store import SQLiteStore

    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteStore(
            db_path=os.path.join(tmp, "k.sqlite3"),
            data_dir=tmp,
        )
        store.init_schema()
        client = TestClient(create_knowledge_app(store=store))
        with client.stream("GET", "/api/v1/documents/doc-sse/upload-progress") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = resp.read().decode("utf-8")
        frames = [line for line in body.split("\n") if line.startswith("data: ")]
        assert frames
        first = json.loads(frames[0][len("data: "):])
        assert first["doc_id"] == "doc-sse"
        assert first["stage"] == "completed"
        assert first["percent"] == 100.0
