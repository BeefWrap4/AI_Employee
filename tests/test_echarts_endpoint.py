"""R19-2: ECharts trend data endpoint tests.

The endpoint ``POST /api/v1/chat/echarts`` accepts
``{session_id, question, metric, window_minutes}`` and returns an ECharts
option dict containing ``xAxis``/``yAxis``/``series``.  The data is aggregated
from existing alarm/KPI sources (rca-agent ``AlarmEvent`` store + KPI
adapter), with pluggable aggregators so tests inject fakes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ai_employee.knowledge_api.echarts import (
    AlarmAggregator,
    EChartsAggregator,
    KpiAggregator,
)
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.rca_agent.kpi_influx import KpiPoint
from fastapi.testclient import TestClient


class _FakeAlarmAgg(AlarmAggregator):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def bucket_alarms(
        self,
        *,
        metric: str,
        window_minutes: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {"metric": metric, "window_minutes": window_minutes, "now": now}
        )
        # 3 buckets spanning the window
        buckets: list[dict[str, Any]] = []
        for i in range(3):
            ts = (now - timedelta(minutes=window_minutes - i * (window_minutes // 3))).isoformat()
            buckets.append({"ts": ts, "count": 1 + i, "severity": "major"})
        return buckets


class _FakeKpiAgg(KpiAggregator):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def bucket_kpi(
        self,
        *,
        metric: str,
        site_id: str,
        window_minutes: int,
        now: datetime,
    ) -> list[KpiPoint]:
        self.calls.append(
            {
                "metric": metric,
                "site_id": site_id,
                "window_minutes": window_minutes,
                "now": now,
            }
        )
        return [
            KpiPoint(
                ts=(now - timedelta(minutes=window_minutes - i * (window_minutes // 3))).isoformat(),
                value=float(10 + i),
                field=metric,
            )
            for i in range(3)
        ]


def _build_client_with_fakes() -> tuple[TestClient, _FakeAlarmAgg, _FakeKpiAgg]:
    from ai_employee.knowledge_api.app import create_app
    from ai_employee.knowledge_api.worker_client import WorkerClient

    alarm_agg = _FakeAlarmAgg()
    kpi_agg = _FakeKpiAgg()
    aggregator = EChartsAggregator(alarm=alarm_agg, kpi=kpi_agg)

    # No worker — we only hit the chat endpoint.
    class _NoopWorker(WorkerClient):
        def __init__(self) -> None:
            self._reachable = False

        def health(self) -> bool:
            return False

        def parse(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            from ai_employee.knowledge_api.worker_client import WorkerDispatchResult

            return WorkerDispatchResult(False, "noop", "noop")

    store = SQLiteStore
    import os
    import tempfile

    td = tempfile.mkdtemp(prefix="r19_echarts_")
    s = store(db_path=os.path.join(td, "k.sqlite3"), data_dir=td)
    s.init_schema()
    app = create_app(store=s, worker_client=_NoopWorker())
    # Inject aggregator on app.state for the endpoint to read.
    app.state.echarts_aggregator = aggregator
    return TestClient(app), alarm_agg, kpi_agg


def test_echarts_returns_xaxis_yaxis_series() -> None:
    client, alarm_agg, _kpi_agg = _build_client_with_fakes()
    resp = client.post(
        "/api/v1/chat/echarts",
        json={
            "session_id": "s-echarts",
            "question": "过去一小时告警趋势",
            "metric": "alarm_count",
            "window_minutes": 30,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ECharts option structure
    assert "xAxis" in body
    assert "yAxis" in body
    assert "series" in body
    assert isinstance(body["xAxis"]["data"], list)
    assert len(body["xAxis"]["data"]) == 3
    assert isinstance(body["series"], list)
    assert body["series"][0]["type"] in {"line", "bar"}
    assert "name" in body["series"][0]
    assert "data" in body["series"][0]
    # Aggregators were invoked with correct metric/window
    assert alarm_agg.calls[0]["metric"] == "alarm_count"
    assert alarm_agg.calls[0]["window_minutes"] == 30


def test_echarts_with_kpi_metric_uses_kpi_aggregator() -> None:
    client, alarm_agg, kpi_agg = _build_client_with_fakes()
    resp = client.post(
        "/api/v1/chat/echarts",
        json={
            "session_id": "s-kpi",
            "question": "KPI 趋势",
            "metric": "prb_util",
            "window_minutes": 15,
            "site_id": "BJ-001",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["series"]) >= 1
    assert len(body["xAxis"]["data"]) == 3
    assert kpi_agg.calls[0]["metric"] == "prb_util"
    assert kpi_agg.calls[0]["site_id"] == "BJ-001"
    # alarm aggregator not invoked for kpi metric
    assert alarm_agg.calls == []


def test_echarts_404_when_no_data() -> None:
    """If the aggregator returns no buckets, return 404 with error code."""
    client, _, _ = _build_client_with_fakes()
    # Patch the fakes to return empty
    app = client.app
    agg = app.state.echarts_aggregator

    class _EmptyAlarm(_FakeAlarmAgg):
        def bucket_alarms(self, **kwargs):  # type: ignore[no-untyped-def]
            return []

    class _EmptyKpi(_FakeKpiAgg):
        def bucket_kpi(self, **kwargs):  # type: ignore[no-untyped-def]
            return []

    app.state.echarts_aggregator = EChartsAggregator(
        alarm=_EmptyAlarm(), kpi=_EmptyKpi()
    )
    resp = client.post(
        "/api/v1/chat/echarts",
        json={
            "session_id": "s-empty",
            "question": "x",
            "metric": "alarm_count",
            "window_minutes": 30,
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "no_trend_data"
