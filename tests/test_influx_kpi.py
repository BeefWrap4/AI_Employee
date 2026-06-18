"""InfluxDB time-series KPI query tests (spec P2 §4 InfluxDB/企业时序库)."""
from __future__ import annotations

import pytest
from ai_employee.rca_agent.kpi_influx import (
    FakeInfluxClient,
    InfluxKpiAdapter,
    KpiPoint,
    KpiQueryResult,
    build_influx_kpi_adapter,
)

# --------------------------------------------------------------------------- #
# KpiPoint / KpiQueryResult
# --------------------------------------------------------------------------- #


def test_kpi_point_to_dict() -> None:
    p = KpiPoint(ts="2026-06-18T10:00:00Z", value=92.5, field="prb_util")
    d = p.to_dict()
    assert d["value"] == 92.5
    assert d["field"] == "prb_util"


def test_kpi_query_result_empty() -> None:
    r = KpiQueryResult(metric="prb_util", site_id="BJ-001", points=[])
    assert r.point_count == 0
    assert r.avg is None
    assert r.max is None


def test_kpi_query_result_stats() -> None:
    r = KpiQueryResult(
        metric="prb_util", site_id="BJ-001",
        points=[
            KpiPoint(ts="t1", value=80.0, field="prb_util"),
            KpiPoint(ts="t2", value=90.0, field="prb_util"),
            KpiPoint(ts="t3", value=100.0, field="prb_util"),
        ],
    )
    assert r.point_count == 3
    assert r.avg == 90.0
    assert r.max == 100.0
    assert r.min == 80.0


def test_kpi_query_result_to_dict() -> None:
    r = KpiQueryResult(
        metric="rrc_fail", site_id="BJ-001",
        points=[KpiPoint(ts="t1", value=5.0, field="rrc_fail")],
    )
    d = r.to_dict()
    assert d["metric"] == "rrc_fail"
    assert d["avg"] == 5.0
    assert d["point_count"] == 1


# --------------------------------------------------------------------------- #
# FakeInfluxClient
# --------------------------------------------------------------------------- #


def test_fake_client_query_returns_seeded_points() -> None:
    client = FakeInfluxClient()
    client.seed("BJ-001", "prb_util", [
        KpiPoint(ts="t1", value=80.0, field="prb_util"),
        KpiPoint(ts="t2", value=90.0, field="prb_util"),
    ])
    points = client.query(metric="prb_util", site_id="BJ-001", window="1h")
    assert len(points) == 2


def test_fake_client_query_missing_returns_empty() -> None:
    client = FakeInfluxClient()
    assert client.query(metric="x", site_id="y", window="1h") == []


# --------------------------------------------------------------------------- #
# InfluxKpiAdapter
# --------------------------------------------------------------------------- #


def test_adapter_query_kpi_returns_result() -> None:
    client = FakeInfluxClient()
    client.seed("BJ-001", "prb_util", [
        KpiPoint(ts="t1", value=80.0, field="prb_util"),
        KpiPoint(ts="t2", value=90.0, field="prb_util"),
    ])
    adapter = InfluxKpiAdapter(client=client)  # type: ignore[arg-type]
    result = adapter.query_kpi(metric="prb_util", site_id="BJ-001", window="1h")
    assert result.point_count == 2
    assert result.avg == 85.0


def test_adapter_query_kpi_empty_returns_empty_result() -> None:
    client = FakeInfluxClient()
    adapter = InfluxKpiAdapter(client=client)  # type: ignore[arg-type]
    result = adapter.query_kpi(metric="x", site_id="y", window="1h")
    assert result.point_count == 0
    assert result.avg is None


def test_adapter_to_evidence_payload() -> None:
    client = FakeInfluxClient()
    client.seed("BJ-001", "rrc_fail", [
        KpiPoint(ts="t1", value=5.0, field="rrc_fail"),
    ])
    adapter = InfluxKpiAdapter(client=client)  # type: ignore[arg-type]
    result = adapter.query_kpi(metric="rrc_fail", site_id="BJ-001", window="1h")
    payload = adapter.to_evidence_payload(result)
    assert "BJ-001" in payload["content"]
    assert payload["source_type"] == "metric"
    assert payload["confidence"] > 0


def test_adapter_query_multiple_metrics() -> None:
    client = FakeInfluxClient()
    client.seed("BJ-001", "prb_util", [
        KpiPoint(ts="t1", value=80.0, field="prb_util"),
    ])
    client.seed("BJ-001", "rrc_fail", [
        KpiPoint(ts="t1", value=3.0, field="rrc_fail"),
        KpiPoint(ts="t2", value=7.0, field="rrc_fail"),
    ])
    adapter = InfluxKpiAdapter(client=client)  # type: ignore[arg-type]
    results = adapter.query_metrics(
        metrics=["prb_util", "rrc_fail"], site_id="BJ-001", window="1h",
    )
    assert len(results) == 2
    by_metric = {r.metric: r for r in results}
    assert by_metric["prb_util"].avg == 80.0
    assert by_metric["rrc_fail"].avg == 5.0


# --------------------------------------------------------------------------- #
# build_influx_kpi_adapter
# --------------------------------------------------------------------------- #


def test_build_adapter_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFLUXDB_URL", raising=False)
    assert build_influx_kpi_adapter() is None


def test_build_adapter_enabled_returns_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFLUXDB_URL", "http://localhost:8086")
    monkeypatch.setenv("INFLUXDB_TOKEN", "dev-token")
    monkeypatch.setenv("INFLUXDB_ORG", "ai-employee")
    monkeypatch.setenv("INFLUXDB_BUCKET", "kpi")
    import ai_employee.rca_agent.kpi_influx as mod

    monkeypatch.setattr(mod, "_connect_influx", lambda **kw: FakeInfluxClient())
    adapter = build_influx_kpi_adapter()
    assert adapter is not None
    assert isinstance(adapter, InfluxKpiAdapter)


def test_build_adapter_unreachable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFLUXDB_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("INFLUXDB_TOKEN", "dev-token")
    monkeypatch.setenv("INFLUXDB_ORG", "ai-employee")
    monkeypatch.setenv("INFLUXDB_BUCKET", "kpi")
    monkeypatch.setenv("INFLUXDB_TIMEOUT_S", "0.2")
    assert build_influx_kpi_adapter() is None
