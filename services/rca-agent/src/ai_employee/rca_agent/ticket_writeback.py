"""Ticket write-back — POST RCA summary back to the ticketing system.

Spec: §6.4 of platform-m6 / RCA.  Real backing service is a ticket API
(Jira / ServiceNow / internal ticketing), gated by ``TICKET_API_URL``.
Default behaviour uses an in-memory fixture so write-back attempts are
auditable in MVP / dev environments.

Each write-back attempt is recorded in :class:`TicketWritebackStore` so
operators can audit failed posts and retry.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from ai_employee.common_schemas.redaction import RedactionConfig, redact_text

DEFAULT_TICKET_API_URL = "http://127.0.0.1:8089/tickets"
_DEFAULT_REDACTION = RedactionConfig()


class TicketWritebackError(Exception):
    """Ticketing system returned non-2xx or the payload was rejected."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"ticket-api returned {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class TicketWritebackUnavailable(Exception):
    """Ticketing system unreachable (network / timeout)."""


class TicketWritebackAdapter(Protocol):
    """Minimal contract for a ticket write-back target."""

    name: str

    def post_summary(
        self,
        ticket_id: str,
        rca_report_id: str,
        incident_id: str,
        summary_markdown: str,
        final_root_cause: str | None,
    ) -> dict[str, Any]: ...


class FixtureTicketWritebackAdapter:
    name = "fixture.ticket_writeback"

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def post_summary(
        self,
        ticket_id: str,
        rca_report_id: str,
        incident_id: str,
        summary_markdown: str,
        final_root_cause: str | None,
    ) -> dict[str, Any]:
        record = {
            "ticket_id": ticket_id,
            "rca_report_id": rca_report_id,
            "incident_id": incident_id,
            "summary_chars": len(summary_markdown),
            "final_root_cause": final_root_cause,
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.posted.append(record)
        return {"ticket_id": ticket_id, "comment_id": f"comment_{len(self.posted):03d}"}


class HttpTicketWritebackAdapter:
    name = "http.ticket_writeback"

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def post_summary(
        self,
        ticket_id: str,
        rca_report_id: str,
        incident_id: str,
        summary_markdown: str,
        final_root_cause: str | None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{ticket_id}/comments"
        # Redact PII / secrets from outbound ticket payloads so we never
        # write phone numbers, emails, ID cards, IPs, IMSI, or password
        # tokens into the ticketing system.
        redacted_summary = redact_text(summary_markdown, _DEFAULT_REDACTION)
        redacted_root_cause = (
            redact_text(final_root_cause, _DEFAULT_REDACTION) if final_root_cause else None
        )
        payload = {
            "rca_report_id": rca_report_id,
            "incident_id": incident_id,
            "final_root_cause": redacted_root_cause,
            "body": redacted_summary,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise TicketWritebackUnavailable(f"{self.base_url} unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise TicketWritebackError(resp.status_code, resp.text)
        if not resp.content:
            return {"ticket_id": ticket_id, "comment_id": "unknown"}
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"ticket_id": ticket_id, "comment_id": resp.text[:64]}


def build_writeback_adapter() -> TicketWritebackAdapter:
    """Pick a real ticket write-back adapter when ``TICKET_API_URL`` is set,
    otherwise return the in-memory fixture adapter.
    """
    url = os.getenv("TICKET_API_URL")
    if url:
        return HttpTicketWritebackAdapter(url)
    return FixtureTicketWritebackAdapter()


# --------------------------------------------------------------------------- #
# Audit log for write-back attempts
# --------------------------------------------------------------------------- #


@dataclass
class TicketWritebackRecord:
    attempt_id: str
    ticket_id: str
    rca_report_id: str
    incident_id: str
    status: str  # "success" | "failed"
    adapter_name: str
    response: dict[str, Any]
    error: str | None
    created_at: str


@dataclass
class TicketWritebackStore:
    records: dict[str, TicketWritebackRecord] = field(default_factory=dict)
    _counter: int = 0

    def record(
        self,
        *,
        ticket_id: str,
        rca_report_id: str,
        incident_id: str,
        status: str,
        adapter_name: str,
        response: dict[str, Any],
        error: str | None,
    ) -> TicketWritebackRecord:
        self._counter += 1
        record = TicketWritebackRecord(
            attempt_id=f"twb_{self._counter:04d}",
            ticket_id=ticket_id,
            rca_report_id=rca_report_id,
            incident_id=incident_id,
            status=status,
            adapter_name=adapter_name,
            response=response,
            error=error,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.records[record.attempt_id] = record
        return record

    def list_for_ticket(self, ticket_id: str) -> list[TicketWritebackRecord]:
        return [r for r in self.records.values() if r.ticket_id == ticket_id]


__all__ = [
    "DEFAULT_TICKET_API_URL",
    "FixtureTicketWritebackAdapter",
    "HttpTicketWritebackAdapter",
    "TicketWritebackAdapter",
    "TicketWritebackError",
    "TicketWritebackRecord",
    "TicketWritebackStore",
    "TicketWritebackUnavailable",
    "build_writeback_adapter",
]
