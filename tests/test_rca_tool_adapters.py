"""RCA tool adapters — pluggable KPI / Log / Topology / Ticket data sources.

Each adapter implements ``fetch(incident) -> list[Evidence]``.  Default
implementation is a deterministic fixture so tests do not require a
running Prometheus / Elasticsearch / Neo4j / ticketing system.  Real
adapters are gated by env vars (PROMETHEUS_ENABLED etc.) and selected
automatically by ``build_adapters``.
"""
from __future__ import annotations

import pytest

from ai_employee.rca_agent.schemas import Evidence, IncidentResponse, RawAlarmEvent
from ai_employee.rca_agent.tool_adapters import (
    ElasticsearchLogAdapter,
    FixtureKPIAdapter,
    FixtureLogAdapter,
    FixtureTicketAdapter,
    FixtureTopologyAdapter,
    Neo4jTopologyAdapter,
    PrometheusKPIAdapter,
    TicketApiAdapter,
    build_adapters,
)


def _make_incident() -> IncidentResponse:
    alarm = RawAlarmEvent(
        alarm_id="a_001",
        alarm_code="LINK_DEGRADE",
        alarm_name="Transmission link degradation",
        vendor="huawei",
        site_id="SITE-001",
        cell_id="CELL-001",
        ne_id="NE-001",
        severity="critical",
        start_time="2026-06-17T10:00:00+08:00",
        raw_payload={"port": "eth0/1"},
    )
    from ai_employee.rca_agent.runtime import normalize_alarm
    from ai_employee.rca_agent.schemas import AlarmEvent

    event = AlarmEvent(**alarm.model_dump(), alarm_event_id="alarm_evt_001",
                       fingerprint=f"{alarm.vendor}:{alarm.site_id}:{alarm.ne_id}:{alarm.alarm_code}")
    return IncidentResponse(
        incident_id="inc_001",
        title="SITE-001 Transmission link degradation",
        status="analyzing",
        severity="critical",
        site_id="SITE-001",
        primary_alarm=event,
        related_alarm_count=0,
        alarm_events=[event],
    )


def test_fixture_kpi_adapter_returns_metric_evidence() -> None:
    incident = _make_incident()
    adapter = FixtureKPIAdapter()
    evidence = adapter.fetch(incident)
    assert evidence
    assert all(item.source_type == "metric" for item in evidence)
    assert all(item.confidence >= 0 and item.confidence <= 1 for item in evidence)
    assert any("SITE-001" in item.source_ref for item in evidence)


def test_fixture_log_adapter_returns_log_evidence() -> None:
    incident = _make_incident()
    adapter = FixtureLogAdapter()
    evidence = adapter.fetch(incident)
    assert evidence
    assert all(item.source_type == "log" for item in evidence)
    assert any("NE-001" in item.source_ref for item in evidence)


def test_fixture_topology_adapter_returns_topology_evidence() -> None:
    incident = _make_incident()
    adapter = FixtureTopologyAdapter()
    evidence = adapter.fetch(incident)
    assert evidence
    assert all(item.source_type == "topology" for item in evidence)
    assert any("SITE-001" in item.source_ref for item in evidence)


def test_fixture_ticket_adapter_returns_ticket_evidence() -> None:
    incident = _make_incident()
    adapter = FixtureTicketAdapter()
    evidence = adapter.fetch(incident)
    assert evidence
    assert all(item.source_type == "ticket" for item in evidence)


def test_build_adapters_defaults_to_all_fixtures(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PROMETHEUS_ENABLED", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_ENABLED", raising=False)
    monkeypatch.delenv("NEO4J_ENABLED", raising=False)
    monkeypatch.delenv("TICKET_API_ENABLED", raising=False)
    monkeypatch.setenv("TICKET_API_FIXTURE_PATH", str(tmp_path / "tickets.json"))
    adapters = build_adapters()
    assert isinstance(adapters["kpi"], FixtureKPIAdapter)
    assert isinstance(adapters["log"], FixtureLogAdapter)
    assert isinstance(adapters["topology"], FixtureTopologyAdapter)
    assert isinstance(adapters["ticket"], FixtureTicketAdapter)
    incident = _make_incident()
    evidence = []
    for adapter in adapters.values():
        evidence.extend(adapter.fetch(incident))
    assert len(evidence) >= 4
    source_types = {item.source_type for item in evidence}
    assert source_types == {"metric", "log", "topology", "ticket"}


def test_build_adapters_returns_real_classes_when_flags_enabled(monkeypatch) -> None:
    """When env flags are enabled, build_adapters returns the real adapter
    class — fixture classes must not be silently substituted.

    The ``fetch`` path is exercised separately in the integration test
    below with a working httpx mock so we do not depend on a live
    Prometheus/Elasticsearch/Neo4j/ticketing service.
    """
    monkeypatch.setenv("PROMETHEUS_ENABLED", "true")
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    monkeypatch.setenv("ELASTICSEARCH_ENABLED", "true")
    monkeypatch.setenv("ELASTICSEARCH_URL", "http://es.local:9200")
    monkeypatch.setenv("NEO4J_ENABLED", "true")
    monkeypatch.setenv("NEO4J_URL", "http://neo4j.local:7474")
    monkeypatch.setenv("TICKET_API_ENABLED", "true")
    monkeypatch.setenv("TICKET_API_URL", "http://ticket.local")
    adapters = build_adapters()
    assert isinstance(adapters["kpi"], PrometheusKPIAdapter)
    assert isinstance(adapters["log"], ElasticsearchLogAdapter)
    assert isinstance(adapters["topology"], Neo4jTopologyAdapter)
    assert isinstance(adapters["ticket"], TicketApiAdapter)


def test_real_adapter_surfaces_unavailable_error(monkeypatch) -> None:
    """When the backing service is unreachable, real adapters must surface
    AdapterUnavailable so callers can decide to fall back to fixture data."""
    from ai_employee.rca_agent.tool_adapters import (
        AdapterUnavailable,
        PrometheusKPIAdapter,
    )

    monkeypatch.setenv("PROMETHEUS_URL", "http://does-not-exist.invalid:9090")
    adapter = PrometheusKPIAdapter("http://does-not-exist.invalid:9090")
    with pytest.raises(AdapterUnavailable):
        adapter.fetch(_make_incident())


def test_real_adapter_returns_evidence_on_success(monkeypatch) -> None:
    """Successful HTTP 200 from backing service is mapped to Evidence."""
    from unittest.mock import MagicMock, patch

    from ai_employee.rca_agent.tool_adapters import PrometheusKPIAdapter

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": {"result": [{"metric": {}}]}}
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.local:9090")
    with patch("httpx.get", return_value=fake_resp):
        adapter = PrometheusKPIAdapter("http://prom.local:9090")
        evidence = adapter.fetch(_make_incident())
    assert len(evidence) == 1
    assert evidence[0].source_type == "metric"
    assert "SITE-001" in evidence[0].source_ref
