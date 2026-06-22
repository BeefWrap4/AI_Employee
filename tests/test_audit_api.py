"""Audit log query API tests (spec P3 §5 全链路审计 + 可观测).

Mounts ``GET /api/v1/audit/events`` (filter by actor / target /
action / tenant / time range) and ``GET /api/v1/audit/export``
(csv/json streaming) on the platform app.  Uses the in-memory
:class:`InMemoryAuditLog` by default; the same endpoint contract
works against OpenSearch via a backend swap (deferred to R14).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ai_employee.agent_platform_api.app import create_app
from ai_employee.agent_platform_api.audit import (
    AuditEvent,
    record_event,
    reset_audit_log,
)
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# filter helpers (pure)
# --------------------------------------------------------------------------- #


def test_filter_by_actor() -> None:
    from ai_employee.agent_platform_api.audit_api import (
        filter_events,
    )

    events = [
        AuditEvent(
            seq=1,
            ts="t1",
            actor="alice",
            action="run.created",
            target_type="agent_run",
            target_id="r1",
        ),
        AuditEvent(
            seq=2,
            ts="t2",
            actor="bob",
            action="run.created",
            target_type="agent_run",
            target_id="r2",
        ),
    ]
    out = filter_events(events, actor="alice")
    assert [e.seq for e in out] == [1]


def test_filter_by_action() -> None:
    from ai_employee.agent_platform_api.audit_api import filter_events

    events = [
        AuditEvent(seq=1, ts="t1", actor="x", action="run.created", target_type="t", target_id="1"),
        AuditEvent(
            seq=2, ts="t2", actor="x", action="approval.decided", target_type="t", target_id="2"
        ),
    ]
    out = filter_events(events, action="approval.decided")
    assert [e.seq for e in out] == [2]


def test_filter_by_target() -> None:
    from ai_employee.agent_platform_api.audit_api import filter_events

    events = [
        AuditEvent(seq=1, ts="t1", actor="x", action="a", target_type="agent_run", target_id="r1"),
        AuditEvent(
            seq=2, ts="t2", actor="x", action="a", target_type="approval_task", target_id="t1"
        ),
    ]
    out = filter_events(events, target_type="agent_run", target_id="r1")
    assert [e.seq for e in out] == [1]


def test_filter_by_time_range() -> None:
    from ai_employee.agent_platform_api.audit_api import filter_events

    events = [
        AuditEvent(
            seq=1, ts="2026-01-01T00:00:00Z", actor="x", action="a", target_type="t", target_id="1"
        ),
        AuditEvent(
            seq=2, ts="2026-06-18T00:00:00Z", actor="x", action="a", target_type="t", target_id="2"
        ),
    ]
    out = filter_events(
        events,
        start_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end_ts=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    assert [e.seq for e in out] == [2]


def test_filter_by_tenant_in_payload() -> None:
    from ai_employee.agent_platform_api.audit_api import filter_events

    events = [
        AuditEvent(
            seq=1,
            ts="t1",
            actor="x",
            action="a",
            target_type="t",
            target_id="1",
            payload={"tenant_id": "acme"},
        ),
        AuditEvent(
            seq=2,
            ts="t2",
            actor="x",
            action="a",
            target_type="t",
            target_id="2",
            payload={"tenant_id": "globex"},
        ),
    ]
    out = filter_events(events, tenant_id="acme")
    assert [e.seq for e in out] == [1]


def test_filter_limit_caps_results() -> None:
    from ai_employee.agent_platform_api.audit_api import filter_events

    events = [
        AuditEvent(seq=i, ts=f"t{i}", actor="x", action="a", target_type="t", target_id=str(i))
        for i in range(20)
    ]
    out = filter_events(events, limit=5)
    assert len(out) == 5


def test_filter_pagination_offset() -> None:
    from ai_employee.agent_platform_api.audit_api import filter_events

    events = [
        AuditEvent(seq=i, ts=f"t{i}", actor="x", action="a", target_type="t", target_id=str(i))
        for i in range(10)
    ]
    out = filter_events(events, limit=3, offset=4)
    assert [e.seq for e in out] == [4, 5, 6]


# --------------------------------------------------------------------------- #
# serialise helpers
# -------------------------------------------------------------------------- #


def test_to_csv_round_trip() -> None:
    from ai_employee.agent_platform_api.audit_api import events_to_csv

    events = [
        AuditEvent(
            seq=1,
            ts="t1",
            actor="alice",
            action="run.created",
            target_type="agent_run",
            target_id="r1",
            payload={"tenant_id": "acme"},
        ),
    ]
    text = events_to_csv(events)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    # Header + one data row.
    assert rows[0] == ["seq", "ts", "actor", "action", "target_type", "target_id", "payload"]
    assert rows[1][0] == "1"
    assert rows[1][2] == "alice"


def test_to_jsonl_round_trip() -> None:
    import json

    from ai_employee.agent_platform_api.audit_api import events_to_jsonl

    events = [
        AuditEvent(
            seq=1,
            ts="t1",
            actor="alice",
            action="run.created",
            target_type="agent_run",
            target_id="r1",
        ),
        AuditEvent(
            seq=2,
            ts="t2",
            actor="bob",
            action="approval.decided",
            target_type="approval_task",
            target_id="t1",
        ),
    ]
    text = events_to_jsonl(events)
    lines = text.strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["actor"] == "alice"
    assert parsed[1]["action"] == "approval.decided"


# --------------------------------------------------------------------------- #
# HTTP endpoints
# -------------------------------------------------------------------------- #


def test_audit_events_endpoint_returns_filtered() -> None:
    reset_audit_log()
    record_event(action="run.created", actor="alice", target_type="agent_run", target_id="r1")
    record_event(
        action="approval.decided", actor="bob", target_type="approval_task", target_id="t1"
    )
    client = TestClient(create_app())
    resp = client.get("/api/v1/audit/events?action=run.created")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) == 1
    assert body["items"][0]["action"] == "run.created"
    assert body["items"][0]["actor"] == "alice"


def test_audit_events_endpoint_filter_by_actor() -> None:
    reset_audit_log()
    record_event(action="run.created", actor="alice", target_type="agent_run", target_id="r1")
    record_event(action="run.created", actor="bob", target_type="agent_run", target_id="r2")
    client = TestClient(create_app())
    resp = client.get("/api/v1/audit/events?actor=alice")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["actor"] == "alice"


def test_audit_events_endpoint_filter_by_tenant() -> None:
    reset_audit_log()
    record_event(
        action="run.created",
        actor="alice",
        target_type="agent_run",
        target_id="r1",
        payload={"tenant_id": "acme"},
    )
    record_event(
        action="run.created",
        actor="bob",
        target_type="agent_run",
        target_id="r2",
        payload={"tenant_id": "globex"},
    )
    client = TestClient(create_app())
    resp = client.get(
        "/api/v1/audit/events?tenant_id=acme",
        headers={"X-Tenant-ID": "acme"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["payload"]["tenant_id"] == "acme"


def test_audit_events_endpoint_pagination() -> None:
    reset_audit_log()
    for i in range(10):
        record_event(action="run.created", actor=f"u{i}", target_type="agent_run", target_id=str(i))
    client = TestClient(create_app())
    resp = client.get("/api/v1/audit/events?limit=3&offset=2")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert resp.json()["total"] == 10


def test_audit_export_csv_endpoint() -> None:
    reset_audit_log()
    record_event(action="run.created", actor="alice", target_type="agent_run", target_id="r1")
    client = TestClient(create_app())
    resp = client.get("/api/v1/audit/export?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    body = resp.text
    assert "seq,ts,actor,action" in body
    assert "alice" in body


def test_audit_export_jsonl_endpoint() -> None:
    reset_audit_log()
    record_event(action="run.created", actor="alice", target_type="agent_run", target_id="r1")
    client = TestClient(create_app())
    resp = client.get("/api/v1/audit/export?format=jsonl")
    assert resp.status_code == 200
    assert "application/x-ndjson" in resp.headers["content-type"]
    lines = resp.text.strip().split("\n")
    assert len(lines) == 1
    import json

    parsed = json.loads(lines[0])
    assert parsed["actor"] == "alice"


def test_audit_export_invalid_format_400() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/v1/audit/export?format=xml")
    # FastAPI returns 422 for query-pattern validation failures.
    assert resp.status_code in (400, 422)
