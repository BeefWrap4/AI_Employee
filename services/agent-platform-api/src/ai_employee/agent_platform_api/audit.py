"""Append-only audit log (spec §5.7).

Records every meaningful platform action — approval decisions, run
creation, tool registration — as an :class:`AuditEvent`.  Events are
immutable once written: appending returns a fresh snapshot, never
mutates an existing one, so downstream consumers can rely on stable
``seq`` numbers.

The default backend (:class:`InMemoryAuditLog`) is process-local and
thread-safe.  Swap in a SQL-backed store via the :class:`AuditLogStore`
protocol for production.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass
class AuditEvent:
    """A single immutable audit record."""

    seq: int
    ts: str
    actor: str
    action: str
    target_type: str
    target_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLogStore(Protocol):
    """Minimal contract for audit-log backends."""

    def append(
        self,
        *,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent: ...

    def list_all(self) -> list[AuditEvent]: ...

    def list_by_actor(self, actor: str) -> list[AuditEvent]: ...

    def list_by_target(
        self, *, target_type: str, target_id: str,
    ) -> list[AuditEvent]: ...

    def list_by_action(self, action: str) -> list[AuditEvent]: ...

    def reset(self) -> None: ...


class InMemoryAuditLog:
    """Thread-safe in-process audit log.

    ``seq`` numbers are assigned atomically when :meth:`append` is
    called, so concurrent appends always get distinct, monotonically
    increasing sequence numbers.
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._next_seq = 1
        self._lock = threading.Lock()

    def append(
        self,
        *,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            event = AuditEvent(
                seq=seq,
                ts=datetime.now(timezone.utc).isoformat(),
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                payload=dict(payload or {}),
            )
            # Snapshot the payload so later mutations don't leak in.
            self._events.append(event)
            return event

    def list_all(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def list_by_actor(self, actor: str) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._events if e.actor == actor]

    def list_by_target(
        self, *, target_type: str, target_id: str,
    ) -> list[AuditEvent]:
        with self._lock:
            return [
                e
                for e in self._events
                if e.target_type == target_type and e.target_id == target_id
            ]

    def list_by_action(self, action: str) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._events if e.action == action]

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._next_seq = 1


# --------------------------------------------------------------------------- #
# Module-level singleton + factory
# --------------------------------------------------------------------------- #

_log = InMemoryAuditLog()


def audit_log() -> AuditLogStore:
    """Return the singleton :class:`AuditLogStore`."""
    return _log  # type: ignore[return-value]


def record_event(
    *,
    action: str,
    actor: str,
    target_type: str,
    target_id: str,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Convenience wrapper around the singleton audit log.

    Automatically attaches ``tenant_id`` from the current request
    context (set by the tenant middleware) so every audit event is
    attributable to a tenant without the caller having to thread it
    through.
    """
    from ai_employee.agent_platform_api.tenant import get_current_tenant_id
    enriched = dict(payload or {})
    enriched.setdefault("tenant_id", get_current_tenant_id())
    return audit_log().append(
        action=action, actor=actor,
        target_type=target_type, target_id=target_id,
        payload=enriched,
    )


def reset_audit_log() -> None:
    """Clear the singleton audit log (test helper)."""
    _log.reset()


__all__ = [
    "AuditEvent",
    "AuditLogStore",
    "InMemoryAuditLog",
    "audit_log",
    "record_event",
    "reset_audit_log",
]