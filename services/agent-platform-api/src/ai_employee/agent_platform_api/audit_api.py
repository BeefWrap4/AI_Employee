"""Audit log query API + export (spec P3 §5 全链路审计).

Mounts ``GET /api/v1/audit/events`` and ``GET /api/v1/audit/export``
on the agent-platform app.  Filters: actor, action, target_type,
target_id, tenant_id (from payload), time range.  Supports pagination
via ``limit`` / ``offset``.  Export formats: ``csv``, ``jsonl``.

Backend is the in-process :class:`InMemoryAuditLog`; swapping in an
OpenSearch backend is a future R14+ concern.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Iterable

from fastapi.responses import PlainTextResponse

from ai_employee.agent_platform_api.audit import AuditEvent


# --------------------------------------------------------------------------- #
# Pure helpers (testable without the FastAPI app)
# --------------------------------------------------------------------------- #


def filter_events(
    events: Iterable[AuditEvent],
    *,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    tenant_id: str | None = None,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditEvent]:
    """Filter + paginate a stream of audit events.

    ``start_ts`` / ``end_ts`` are inclusive bounds (compared against
    :attr:`AuditEvent.ts` parsed as ISO-8601).  The :class:`TenantContext`
    filters by ``payload['tenant_id']``.
    """
    out: list[AuditEvent] = []
    for ev in events:
        if actor and ev.actor != actor:
            continue
        if action and ev.action != action:
            continue
        if target_type and ev.target_type != target_type:
            continue
        if target_id and ev.target_id != target_id:
            continue
        if tenant_id and ev.payload.get("tenant_id") != tenant_id:
            continue
        if start_ts or end_ts:
            try:
                ts = datetime.fromisoformat(ev.ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start_ts and ts < start_ts:
                continue
            if end_ts and ts > end_ts:
                continue
        out.append(ev)
    return out[offset : offset + limit]


def events_to_csv(events: Iterable[AuditEvent]) -> str:
    """Render events as CSV (header + rows)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["seq", "ts", "actor", "action", "target_type", "target_id", "payload"])
    for ev in events:
        writer.writerow([
            ev.seq, ev.ts, ev.actor, ev.action,
            ev.target_type, ev.target_id,
            json.dumps(ev.payload, ensure_ascii=False, default=str),
        ])
    return buf.getvalue()


def events_to_jsonl(events: Iterable[AuditEvent]) -> str:
    """Render events as JSON Lines."""
    return "\n".join(
        json.dumps(asdict(ev), ensure_ascii=False, default=str)
        for ev in events
    )


# --------------------------------------------------------------------------- #
# FastAPI endpoint mounting
# --------------------------------------------------------------------------- #


def mount_audit_endpoints(app: Any) -> None:
    """Mount ``/api/v1/audit/events`` + ``/api/v1/audit/export``.

    Uses the singleton :func:`audit_log` so events produced anywhere
    in the process are visible without explicit wiring.
    """
    from fastapi import HTTPException, Query
    from fastapi.responses import PlainTextResponse

    from ai_employee.agent_platform_api.audit import audit_log

    @app.get("/api/v1/audit/events")
    def list_audit_events(
        actor: str | None = Query(None),
        action: str | None = Query(None),
        target_type: str | None = Query(None),
        target_id: str | None = Query(None),
        tenant_id: str | None = Query(None),
        start_ts: datetime | None = Query(None),
        end_ts: datetime | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        # Read the full list once; ordering matches seq (insertion order).
        events = audit_log().list_all()
        filtered = filter_events(
            events,
            actor=actor, action=action,
            target_type=target_type, target_id=target_id,
            tenant_id=tenant_id,
            start_ts=start_ts, end_ts=end_ts,
            limit=limit, offset=offset,
        )
        # Total after filtering, before pagination — so the client can
        # render "showing X of Y".
        after_filter = filter_events(
            events,
            actor=actor, action=action,
            target_type=target_type, target_id=target_id,
            tenant_id=tenant_id,
            start_ts=start_ts, end_ts=end_ts,
            limit=10_000_000, offset=0,
        )
        return {
            "items": [ev.to_dict() for ev in filtered],
            "total": len(after_filter),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/audit/export")
    def export_audit_events(
        format: str = Query("csv", pattern="^(csv|jsonl)$"),
        actor: str | None = Query(None),
        action: str | None = Query(None),
        target_type: str | None = Query(None),
        target_id: str | None = Query(None),
        tenant_id: str | None = Query(None),
    ) -> PlainTextResponse:
        events = audit_log().list_all()
        filtered = filter_events(
            events,
            actor=actor, action=action,
            target_type=target_type, target_id=target_id,
            tenant_id=tenant_id,
            limit=10_000_000, offset=0,
        )
        if format == "csv":
            return PlainTextResponse(
                events_to_csv(filtered),
                media_type="text/csv",
            )
        return PlainTextResponse(
            events_to_jsonl(filtered),
            media_type="application/x-ndjson",
        )


__all__ = [
    "events_to_csv",
    "events_to_jsonl",
    "filter_events",
    "mount_audit_endpoints",
]