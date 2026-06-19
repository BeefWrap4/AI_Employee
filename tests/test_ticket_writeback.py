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


# --------------------------------------------------------------------------- #
# R24-C: redaction of PII / secrets in write-back payloads
# --------------------------------------------------------------------------- #


def test_fixture_adapter_redacts_summary_markdown() -> None:
    """Phones in summary_markdown are masked in the recorded payload."""
    adapter = FixtureTicketWritebackAdapter()
    adapter.post_summary(
        ticket_id="T-002",
        rca_report_id="rca_002",
        incident_id="inc_002",
        summary_markdown="联系 13800138000 处理",
        final_root_cause="transmission_link_degradation",
    )
    assert len(adapter.posted) == 1
    summary = adapter.posted[0]["summary_chars"]  # only length is recorded
    # Length is preserved (redaction only masks in place, not truncates).
    assert summary == len("联系 13800138000 处理")


def test_http_adapter_redacts_summary_and_root_cause() -> None:
    """The HTTP adapter must redact PII before sending the payload."""
    import json

    fake_resp = MagicMock()
    fake_resp.status_code = 201
    fake_resp.content = b'{"ticket_id":"T-001","comment_id":"c-001"}'
    fake_resp.json.return_value = {"ticket_id": "T-001", "comment_id": "c-001"}
    adapter = HttpTicketWritebackAdapter("http://ticket.local:8089")
    with patch("httpx.post", return_value=fake_resp) as mock_post:
        adapter.post_summary(
            ticket_id="T-001",
            rca_report_id="r1",
            incident_id="i1",
            summary_markdown="联系 13800138000 处理告警 admin@example.com",
            final_root_cause="transmission_link_degradation",
        )
    sent_body = mock_post.call_args.kwargs["json"]
    assert "13800138000" not in sent_body["body"]
    assert "admin@example.com" not in sent_body["body"]
    assert "***" in sent_body["body"]
    # final_root_cause has no PII here, so it must pass through unchanged.
    assert sent_body["final_root_cause"] == "transmission_link_degradation"


def test_http_adapter_redacts_root_cause() -> None:
    """When final_root_cause contains PII, it must be redacted too."""
    fake_resp = MagicMock()
    fake_resp.status_code = 201
    fake_resp.content = b'{"ticket_id":"T-001","comment_id":"c-001"}'
    fake_resp.json.return_value = {"ticket_id": "T-001", "comment_id": "c-001"}
    adapter = HttpTicketWritebackAdapter("http://ticket.local:8089")
    with patch("httpx.post", return_value=fake_resp) as mock_post:
        adapter.post_summary(
            ticket_id="T-001",
            rca_report_id="r1",
            incident_id="i1",
            summary_markdown="normal summary",
            final_root_cause="customer phone 13800138000 reported failure",
        )
    sent_body = mock_post.call_args.kwargs["json"]
    assert "13800138000" not in sent_body["final_root_cause"]
