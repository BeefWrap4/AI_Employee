"""Audit log / event sourcing tests."""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.audit import (
    AuditEvent,
    AuditLogStore,
    InMemoryAuditLog,
)


# --------------------------------------------------------------------------- #
# AuditEvent
# --------------------------------------------------------------------------- #


def test_audit_event_serializes_to_dict() -> None:
    ev = AuditEvent(
        seq=1,
        ts="2026-06-18T00:00:00Z",
        actor="alice",
        action="approval.decided",
        target_type="approval_task",
        target_id="approval_task_001",
        payload={"decision": "approved"},
    )
    d = ev.to_dict()
    assert d["seq"] == 1
    assert d["actor"] == "alice"
    assert d["payload"]["decision"] == "approved"


def test_audit_event_defaults() -> None:
    ev = AuditEvent(
        seq=2, ts="2026-06-18T00:00:01Z",
        actor="bob", action="run.created",
        target_type="agent_run", target_id="agent_run_001",
    )
    assert ev.payload == {}


# --------------------------------------------------------------------------- #
# InMemoryAuditLog
# --------------------------------------------------------------------------- #


def test_audit_log_appends_in_order() -> None:
    log = InMemoryAuditLog()
    e1 = log.append(action="run.created", actor="alice", target_type="agent_run", target_id="r1")
    e2 = log.append(action="run.completed", actor="alice", target_type="agent_run", target_id="r1")
    assert e1.seq == 1
    assert e2.seq == 2
    assert log.list_all() == [e1, e2]


def test_audit_log_list_by_actor() -> None:
    log = InMemoryAuditLog()
    log.append(action="a", actor="alice", target_type="t", target_id="1")
    log.append(action="b", actor="bob", target_type="t", target_id="2")
    log.append(action="c", actor="alice", target_type="t", target_id="3")
    alice = log.list_by_actor("alice")
    assert len(alice) == 2
    assert [e.action for e in alice] == ["a", "c"]


def test_audit_log_list_by_target() -> None:
    log = InMemoryAuditLog()
    log.append(action="a", actor="alice", target_type="agent_run", target_id="r1")
    log.append(action="b", actor="bob", target_type="agent_run", target_id="r2")
    log.append(action="c", actor="alice", target_type="agent_run", target_id="r1")
    r1 = log.list_by_target(target_type="agent_run", target_id="r1")
    assert len(r1) == 2
    assert all(e.target_id == "r1" for e in r1)


def test_audit_log_is_immutable_after_append() -> None:
    """Appended events are snapshots; mutating the returned dict shouldn't
    affect subsequent reads."""
    log = InMemoryAuditLog()
    log.append(action="a", actor="alice", target_type="t", target_id="1", payload={"k": 1})
    event = log.list_all()[0]
    snap = event.to_dict()
    snap["payload"]["k"] = 999  # mutate the snapshot
    again = log.list_all()[0]
    assert again.payload["k"] == 1


def test_audit_log_monotonic_seq_across_append() -> None:
    log = InMemoryAuditLog()
    seqs = []
    for i in range(10):
        ev = log.append(action="x", actor="a", target_type="t", target_id=str(i))
        seqs.append(ev.seq)
    assert seqs == list(range(1, 11))


def test_audit_log_filter_by_action() -> None:
    log = InMemoryAuditLog()
    log.append(action="approval.decided", actor="alice", target_type="approval_task", target_id="t1")
    log.append(action="approval.routed", actor="alice", target_type="approval_task", target_id="t1")
    log.append(action="approval.decided", actor="bob", target_type="approval_task", target_id="t2")
    decided = log.list_by_action("approval.decided")
    assert len(decided) == 2


def test_audit_log_reset_clears_all() -> None:
    log = InMemoryAuditLog()
    log.append(action="x", actor="a", target_type="t", target_id="1")
    log.reset()
    assert log.list_all() == []


def test_audit_log_contains_at_least_one_default() -> None:
    """Empty store should not crash on list calls."""
    log = InMemoryAuditLog()
    assert log.list_all() == []
    assert log.list_by_actor("nobody") == []


# --------------------------------------------------------------------------- #
# AuditLogStore protocol
# --------------------------------------------------------------------------- #


def test_audit_log_satisfies_protocol() -> None:
    log = InMemoryAuditLog()
    # Protocol fields are exercised via the explicit interface methods.
    store: AuditLogStore = log  # type: ignore[assignment]
    assert hasattr(store, "append")
    assert hasattr(store, "list_all")
    assert hasattr(store, "list_by_actor")
    assert hasattr(store, "list_by_target")
    assert hasattr(store, "list_by_action")
    assert hasattr(store, "reset")