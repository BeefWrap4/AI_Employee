"""Enterprise ticketing / CMDB / IM adapter tests (spec P3 §4)."""

from __future__ import annotations

import pytest
from ai_employee.rca_agent.enterprise_adapters import (
    CMDBAdapter,
    CMDBAsset,
    FakeCMDBClient,
    FakeIMClient,
    FakeTicketClient,
    HttpCMDBClient,
    HttpIMClient,
    HttpTicketClient,
    IMMessage,
    TicketRecord,
    build_cmdb_adapter,
    build_im_adapter,
    build_ticket_adapter,
)

# --------------------------------------------------------------------------- #
# CMDB
# --------------------------------------------------------------------------- #


def test_cmdb_asset_to_dict() -> None:
    a = CMDBAsset(
        asset_id="BJ-001",
        name="北京基站-001",
        asset_type="base_station",
        site_id="BJ-001",
        vendor="Huawei",
        model="AAU5613",
        status="active",
    )
    d = a.to_dict()
    assert d["asset_id"] == "BJ-001"
    assert d["vendor"] == "Huawei"


def test_cmdb_query_assets() -> None:
    client = FakeCMDBClient()
    client.seed(
        [
            CMDBAsset(
                asset_id="BJ-001",
                name="BJ-001",
                asset_type="base_station",
                site_id="BJ-001",
                vendor="Huawei",
                model="AAU5613",
                status="active",
            ),
            CMDBAsset(
                asset_id="BJ-002",
                name="BJ-002",
                asset_type="base_station",
                site_id="BJ-002",
                vendor="ZTE",
                model="R8894E",
                status="active",
            ),
        ]
    )
    adapter = CMDBAdapter(client=client)  # type: ignore[arg-type]
    results = adapter.query_assets(site_id="BJ-001")
    assert len(results) == 1
    assert results[0].asset_id == "BJ-001"


def test_cmdb_query_by_vendor() -> None:
    client = FakeCMDBClient()
    client.seed(
        [
            CMDBAsset(
                asset_id="BJ-001",
                name="BJ-001",
                asset_type="base_station",
                site_id="BJ-001",
                vendor="Huawei",
                model="AAU5613",
                status="active",
            ),
            CMDBAsset(
                asset_id="BJ-002",
                name="BJ-002",
                asset_type="base_station",
                site_id="BJ-002",
                vendor="Huawei",
                model="R8894E",
                status="active",
            ),
        ]
    )
    adapter = CMDBAdapter(client=client)  # type: ignore[arg-type]
    results = adapter.query_assets(vendor="Huawei")
    assert len(results) == 2


def test_cmdb_query_empty() -> None:
    client = FakeCMDBClient()
    adapter = CMDBAdapter(client=client)  # type: ignore[arg-type]
    assert adapter.query_assets(site_id="nope") == []


def test_build_cmdb_adapter_with_fake_client() -> None:
    from ai_employee.rca_agent.enterprise_adapters import (
        FakeCMDBClient,
    )

    client = FakeCMDBClient()
    client.add_asset(CMDBAsset("a1", "A1", "base_station", "s1", "H", "M1", "active"))
    adapter = build_cmdb_adapter(client=client)  # type: ignore[arg-type]
    assert len(adapter.query_assets(site_id="s1")) == 1


# --------------------------------------------------------------------------- #
# Ticket
# --------------------------------------------------------------------------- #


def test_ticket_record_to_dict() -> None:
    t = TicketRecord(
        ticket_id="T-1001",
        title="RRC failure on BJ-001",
        status="open",
        priority="high",
        severity="critical",
        source_report_id="rca-r-001",
        assignee=None,
        created_at="2026-06-18T00:00:00Z",
    )
    d = t.to_dict()
    assert d["ticket_id"] == "T-1001"
    assert d["source_report_id"] == "rca-r-001"


def test_ticket_adapter_create_ticket() -> None:
    client = FakeTicketClient()
    adapter = build_ticket_adapter(client=client)  # type: ignore[arg-type]
    ticket = adapter.create_ticket(
        title="RRC failure BJ-001",
        source_report_id="rca-1",
        priority="high",
        severity="critical",
        description="auto-created",
    )
    assert ticket.ticket_id.startswith("T-")
    assert ticket.source_report_id == "rca-1"
    assert client.tickets  # the fake recorded it


def test_ticket_adapter_get_ticket() -> None:
    client = FakeTicketClient()
    adapter = build_ticket_adapter(client=client)  # type: ignore[arg-type]
    t = adapter.create_ticket(title="x", source_report_id="r-1")
    fetched = adapter.get_ticket(t.ticket_id)
    assert fetched is not None
    assert fetched.ticket_id == t.ticket_id


def test_ticket_adapter_write_back_rca_report() -> None:
    """The high-level ``write_back_report`` ties a RCA report to a ticket."""
    client = FakeTicketClient()
    adapter = build_ticket_adapter(client=client)  # type: ignore[arg-type]
    ticket = adapter.write_back_report(
        report_id="rca-r-1",
        title="RRC failure BJ-001",
        report_summary="PRB 高导致 RRC 失败",
        priority="high",
        severity="critical",
    )
    assert ticket.source_report_id == "rca-r-1"
    assert ticket.payload.get("report_summary") == "PRB 高导致 RRC 失败"


# --------------------------------------------------------------------------- #
# IM
# -------------------------------------------------------------------------- #


def test_im_message_to_dict() -> None:
    m = IMMessage(
        channel="#ops-incidents",
        text="alarm spike",
        severity="critical",
        sender="rca-agent",
        ts="2026-06-18T00:00:00Z",
    )
    d = m.to_dict()
    assert d["channel"] == "#ops-incidents"
    assert d["severity"] == "critical"


def test_im_adapter_send_message() -> None:
    client = FakeIMClient()
    adapter = build_im_adapter(client=client)  # type: ignore[arg-type]
    msg = adapter.send(
        channel="#ops-incidents",
        text="RCA 报告已生成",
        severity="high",
    )
    assert msg.text == "RCA 报告已生成"
    assert client.messages[-1].channel == "#ops-incidents"


def test_im_adapter_notify_incident() -> None:
    """High-level ``notify_incident`` formats the channel/text for ops."""
    client = FakeIMClient()
    adapter = build_im_adapter(client=client)  # type: ignore[arg-type]
    msg = adapter.notify_incident(
        incident_id="inc-001",
        title="BJ-001 告警激增",
        severity="critical",
        ticket_id="T-1001",
    )
    assert "inc-001" in msg.text
    assert "T-1001" in msg.text
    assert msg.channel == "#ops-incidents"


# --------------------------------------------------------------------------- #
# HTTP-backed client (real path; tests use a fake to avoid network)
# -------------------------------------------------------------------------- #


def test_http_cmdb_client_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMDB_API_URL", "http://cmdb.example.com")
    client = HttpCMDBClient()
    assert client.base_url == "http://cmdb.example.com"


def test_http_ticket_client_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TICKET_API_URL", "http://ticket.example.com")
    monkeypatch.setenv("TICKET_API_TOKEN", "tkn")
    client = HttpTicketClient()
    assert client.base_url == "http://ticket.example.com"


def test_http_im_client_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IM_WEBHOOK_URL", "http://im.example.com/hook")
    client = HttpIMClient()
    assert client.webhook_url == "http://im.example.com/hook"
