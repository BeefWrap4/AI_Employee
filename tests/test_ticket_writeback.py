"""RCA ticket write-back tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from ai_employee.rca_agent.ticket_writeback import (
    FixtureTicketWritebackAdapter,
    HttpTicketWritebackAdapter,
    TicketWritebackError,
    TicketWritebackStore,
    TicketWritebackUnavailable,
    build_writeback_adapter,
)


def test_fixture_adapter_records_post() -> None:
    adapter = FixtureTicketWritebackAdapter()
    result = adapter.post_summary(
        ticket_id="T-001",
        rca_report_id="rca_report_001",
        incident_id="inc_001",
        summary_markdown="# RCA\n- e_001 foo",
        final_root_cause="transmission_link_degradation",
    )
    assert result["ticket_id"] == "T-001"
    assert len(adapter.posted) == 1
    assert adapter.posted[0]["final_root_cause"] == "transmission_link_degradation"


def test_build_returns_fixture_when_no_url(monkeypatch) -> None:
    monkeypatch.delenv("TICKET_API_URL", raising=False)
    adapter = build_writeback_adapter()
    assert isinstance(adapter, FixtureTicketWritebackAdapter)


def test_build_returns_http_when_url_set(monkeypatch) -> None:
    monkeypatch.setenv("TICKET_API_URL", "http://ticket.local:8089")
    adapter = build_writeback_adapter()
    assert isinstance(adapter, HttpTicketWritebackAdapter)


def test_http_adapter_surfaces_unavailable_on_network_error() -> None:
    adapter = HttpTicketWritebackAdapter("http://does-not-exist.invalid:8089")
    with pytest.raises(TicketWritebackUnavailable):
        adapter.post_summary(
            ticket_id="T-001",
            rca_report_id="rca_report_001",
            incident_id="inc_001",
            summary_markdown="x",
            final_root_cause="foo",
        )


def test_http_adapter_returns_error_on_4xx() -> None:
    fake_resp = MagicMock()
    fake_resp.status_code = 404
    fake_resp.text = "ticket not found"
    adapter = HttpTicketWritebackAdapter("http://ticket.local:8089")
    with patch("httpx.post", return_value=fake_resp):
        with pytest.raises(TicketWritebackError) as ei:
            adapter.post_summary(
                ticket_id="T-001",
                rca_report_id="r1",
                incident_id="i1",
                summary_markdown="x",
                final_root_cause="foo",
            )
    assert ei.value.status_code == 404


def test_http_adapter_returns_comment_id_on_201() -> None:
    fake_resp = MagicMock()
    fake_resp.status_code = 201
    fake_resp.content = b'{"ticket_id":"T-001","comment_id":"c-001"}'
    fake_resp.json.return_value = {"ticket_id": "T-001", "comment_id": "c-001"}
    adapter = HttpTicketWritebackAdapter("http://ticket.local:8089")
    with patch("httpx.post", return_value=fake_resp):
        result = adapter.post_summary(
            ticket_id="T-001",
            rca_report_id="r1",
            incident_id="i1",
            summary_markdown="x",
            final_root_cause="foo",
        )
    assert result["comment_id"] == "c-001"


def test_store_records_attempts() -> None:
    store = TicketWritebackStore()
    r1 = store.record(
        ticket_id="T-001",
        rca_report_id="r1",
        incident_id="i1",
        status="success",
        adapter_name="fixture.ticket_writeback",
        response={"comment_id": "c-001"},
        error=None,
    )
    r2 = store.record(
        ticket_id="T-001",
        rca_report_id="r2",
        incident_id="i2",
        status="failed",
        adapter_name="http.ticket_writeback",
        response={},
        error="connection refused",
    )
    assert r1.attempt_id != r2.attempt_id
    matches = store.list_for_ticket("T-001")
    assert len(matches) == 2
    assert {m.status for m in matches} == {"success", "failed"}
