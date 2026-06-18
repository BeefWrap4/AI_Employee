"""Enterprise ticketing / CMDB / IM adapters (spec P3 §4 企业工单系统/CMDB/IM).

Three adapters that bridge the RCA / agent-platform outputs to
enterprise operational systems:

* :class:`CMDBAdapter` — query assets (基站/小区/传输链路) by site or
  vendor.  Backed by a pluggable client; production uses
  :class:`HttpCMDBClient` (REST), tests use :class:`FakeCMDBClient`.
* :class:`TicketAdapter` — create / fetch tickets.  Used to write back
  RCA reports so the on-call engineer gets a trackable work item
  with the report attached.
* :class:`IMAdapter` — push incident notifications to the team's
  IM channel (e.g. Lark, Slack, DingTalk).

All three follow the same pluggable client pattern: ``adapter`` takes
an injected client, ``build_*_adapter`` wires a default client from
env vars.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx


# --------------------------------------------------------------------------- #
# CMDB
# --------------------------------------------------------------------------- #


@dataclass
class CMDBAsset:
    """One CMDB asset record."""

    asset_id: str
    name: str
    asset_type: str
    site_id: str
    vendor: str
    model: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CMDBClient(Protocol):
    def list_assets(self, *, site_id: str | None, vendor: str | None) -> list[CMDBAsset]: ...


class FakeCMDBClient:
    """In-memory CMDB client for tests."""

    def __init__(self) -> None:
        self._assets: list[CMDBAsset] = []
        self._lock = threading.Lock()

    def add_asset(self, asset: CMDBAsset) -> None:
        with self._lock:
            self._assets.append(asset)

    def seed(self, assets: list[CMDBAsset]) -> None:
        with self._lock:
            self._assets = list(assets)

    def list_assets(self, *, site_id: str | None = None, vendor: str | None = None) -> list[CMDBAsset]:
        with self._lock:
            return [
                a for a in self._assets
                if (site_id is None or a.site_id == site_id)
                and (vendor is None or a.vendor == vendor)
            ]


class HttpCMDBClient:
    """REST client for the enterprise CMDB API."""

    def __init__(self, base_url: str | None = None, *, timeout_s: float = 5.0) -> None:
        self.base_url = (
            base_url or os.environ.get("CMDB_API_URL", "http://localhost:8081")
        ).rstrip("/")
        self._timeout_s = timeout_s

    def list_assets(self, *, site_id: str | None = None, vendor: str | None = None) -> list[CMDBAsset]:
        params: dict[str, str] = {}
        if site_id:
            params["site_id"] = site_id
        if vendor:
            params["vendor"] = vendor
        resp = httpx.get(f"{self.base_url}/api/v1/assets", params=params, timeout=self._timeout_s)
        resp.raise_for_status()
        return [CMDBAsset(**item) for item in resp.json().get("items", [])]


class CMDBAdapter:
    """Domain wrapper around a :class:`CMDBClient`."""

    def __init__(self, *, client: CMDBClient) -> None:
        self._client = client

    def query_assets(
        self, *, site_id: str | None = None, vendor: str | None = None,
    ) -> list[CMDBAsset]:
        return self._client.list_assets(site_id=site_id, vendor=vendor)


def build_cmdb_adapter(
    *,
    base_url: str | None = None,
    client: CMDBClient | None = None,
) -> CMDBAdapter:
    """Build a CMDB adapter; uses :class:`HttpCMDBClient` when no client given."""
    if client is None:
        client = HttpCMDBClient(base_url=base_url)
    return CMDBAdapter(client=client)


# --------------------------------------------------------------------------- #
# Ticket
# --------------------------------------------------------------------------- #


@dataclass
class TicketRecord:
    """One ticket record returned by the ticketing API."""

    ticket_id: str
    title: str
    status: str
    priority: str
    severity: str
    source_report_id: str
    assignee: str | None
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TicketClient(Protocol):
    def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, ticket_id: str) -> dict[str, Any] | None: ...


class FakeTicketClient:
    """In-memory ticket client for tests."""

    def __init__(self) -> None:
        self.tickets: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        ticket = {
            "ticket_id": payload.get("ticket_id") or f"T-{uuid.uuid4().hex[:6]}",
            "title": payload.get("title", ""),
            "status": payload.get("status", "open"),
            "priority": payload.get("priority", "medium"),
            "severity": payload.get("severity", "major"),
            "source_report_id": payload.get("source_report_id", ""),
            "assignee": payload.get("assignee"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        with self._lock:
            self.tickets.append(ticket)
        return ticket

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self._lock:
            for t in self.tickets:
                if t["ticket_id"] == ticket_id:
                    return t
        return None


class HttpTicketClient:
    """REST client for the enterprise ticketing API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("TICKET_API_URL", "http://localhost:8082")
        ).rstrip("/")
        self._token = token or os.environ.get("TICKET_API_TOKEN", "")
        self._timeout_s = timeout_s

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        resp = httpx.post(
            f"{self.base_url}/api/v1/tickets",
            headers=headers, json=payload, timeout=self._timeout_s,
        )
        resp.raise_for_status()
        return resp.json()

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        resp = httpx.get(
            f"{self.base_url}/api/v1/tickets/{ticket_id}",
            headers=headers, timeout=self._timeout_s,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


class TicketAdapter:
    """Domain wrapper around a :class:`TicketClient`."""

    def __init__(self, *, client: TicketClient) -> None:
        self._client = client

    def create_ticket(
        self,
        *,
        title: str,
        source_report_id: str,
        priority: str = "medium",
        severity: str = "major",
        description: str = "",
    ) -> TicketRecord:
        body = self._client.create({
            "title": title,
            "source_report_id": source_report_id,
            "priority": priority,
            "severity": severity,
            "description": description,
        })
        return TicketRecord(**body)

    def get_ticket(self, ticket_id: str) -> TicketRecord | None:
        body = self._client.get(ticket_id)
        return TicketRecord(**body) if body else None

    def write_back_report(
        self,
        *,
        report_id: str,
        title: str,
        report_summary: str,
        priority: str = "high",
        severity: str = "critical",
    ) -> TicketRecord:
        """Create a ticket that references the RCA report id."""
        body = self._client.create({
            "title": title,
            "source_report_id": report_id,
            "priority": priority,
            "severity": severity,
            "description": f"RCA report {report_id}: {report_summary}",
            "report_summary": report_summary,
        })
        return TicketRecord(**body)


def build_ticket_adapter(
    *,
    base_url: str | None = None,
    client: TicketClient | None = None,
) -> TicketAdapter:
    if client is None:
        client = HttpTicketClient(base_url=base_url)
    return TicketAdapter(client=client)


# --------------------------------------------------------------------------- #
# IM
# --------------------------------------------------------------------------- #


@dataclass
class IMMessage:
    channel: str
    text: str
    severity: str
    sender: str
    ts: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IMClient(Protocol):
    def post(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class FakeIMClient:
    def __init__(self) -> None:
        self.messages: list[IMMessage] = []
        self._lock = threading.Lock()

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg = IMMessage(
            channel=payload.get("channel", ""),
            text=payload.get("text", ""),
            severity=payload.get("severity", "info"),
            sender=payload.get("sender", "ai-employee"),
            ts=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self.messages.append(msg)
        return {"ok": True, "ts": msg.ts}


class HttpIMClient:
    """Webhook poster to the enterprise IM platform (Slack/Lark/DingTalk)."""

    def __init__(self, webhook_url: str | None = None, *, timeout_s: float = 5.0) -> None:
        self.webhook_url = (
            webhook_url or os.environ.get("IM_WEBHOOK_URL", "http://localhost:8083/hook")
        )
        self._timeout_s = timeout_s

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = httpx.post(
            self.webhook_url, json=payload, timeout=self._timeout_s,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


class IMAdapter:
    def __init__(self, *, client: IMClient) -> None:
        self._client = client

    def send(
        self, *, channel: str, text: str, severity: str = "info",
        sender: str = "ai-employee",
    ) -> IMMessage:
        body = self._client.post({
            "channel": channel, "text": text, "severity": severity, "sender": sender,
        })
        return IMMessage(
            channel=channel, text=text, severity=severity, sender=sender,
            ts=body.get("ts", datetime.now(timezone.utc).isoformat()),
        )

    def notify_incident(
        self,
        *,
        incident_id: str,
        title: str,
        severity: str = "critical",
        ticket_id: str | None = None,
        channel: str = "#ops-incidents",
    ) -> IMMessage:
        text = f"[{severity.upper()}] {title} (incident={incident_id}"
        if ticket_id:
            text += f", ticket={ticket_id}"
        text += ")"
        sev = "critical" if severity == "critical" else "warning"
        return self.send(channel=channel, text=text, severity=sev)


def build_im_adapter(
    *,
    webhook_url: str | None = None,
    client: IMClient | None = None,
) -> IMAdapter:
    if client is None:
        client = HttpIMClient(webhook_url=webhook_url)
    return IMAdapter(client=client)


__all__ = [
    "CMDBAdapter",
    "CMDBAsset",
    "CMDBClient",
    "FakeCMDBClient",
    "FakeIMClient",
    "FakeTicketClient",
    "HttpCMDBClient",
    "HttpIMClient",
    "HttpTicketClient",
    "IMAdapter",
    "IMClient",
    "IMMessage",
    "TicketAdapter",
    "TicketClient",
    "TicketRecord",
    "build_cmdb_adapter",
    "build_im_adapter",
    "build_ticket_adapter",
]