"""ECharts trend data aggregation (R19-2).

The ``/api/v1/chat/echarts`` endpoint returns an ECharts option dict whose
``xAxis``/``yAxis``/``series`` are computed from existing alarm + KPI
sources.  Aggregation is pluggable so tests can inject fakes:

- :class:`AlarmAggregator` queries ``AlarmEvent`` data (rca-agent store).
- :class:`KpiAggregator` queries KPI time-series (InfluxDB / fake).

The :class:`EChartsAggregator` composes them and emits the standard
ECharts option dict.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class _Bucket:
    ts: str
    value: float
    name: str


class AlarmAggregator(ABC):
    """Aggregate ``AlarmEvent`` rows into (timestamp, count) buckets."""

    @abstractmethod
    def bucket_alarms(
        self, *, metric: str, window_minutes: int, now: datetime
    ) -> list[dict[str, Any]]: ...


class KpiAggregator(ABC):
    """Aggregate KPI time-series into ``KpiPoint`` buckets."""

    @abstractmethod
    def bucket_kpi(
        self, *, metric: str, site_id: str, window_minutes: int, now: datetime
    ) -> list[Any]: ...


# Metrics routed through the alarm aggregator.
_ALARM_METRICS = {"alarm_count", "alarm_rate", "alarm_severity"}


class EChartsAggregator:
    def __init__(self, *, alarm: AlarmAggregator, kpi: KpiAggregator) -> None:
        self.alarm = alarm
        self.kpi = kpi

    def build_option(
        self,
        *,
        metric: str,
        site_id: str | None,
        window_minutes: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if metric in _ALARM_METRICS:
            buckets = self.alarm.bucket_alarms(
                metric=metric, window_minutes=window_minutes, now=now
            )
            xs = [b["ts"] for b in buckets]
            ys = [b.get("count", 0) for b in buckets]
            series_name = f"{metric}"
        else:
            points = self.kpi.bucket_kpi(
                metric=metric,
                site_id=site_id or "default",
                window_minutes=window_minutes,
                now=now,
            )
            xs = [getattr(p, "ts", "") for p in points]
            ys = [getattr(p, "value", 0.0) for p in points]
            series_name = f"{metric}@{site_id or 'default'}"
        if not xs:
            return {}
        return {
            "xAxis": {
                "type": "category",
                "data": xs,
            },
            "yAxis": {
                "type": "value",
            },
            "series": [
                {
                    "name": series_name,
                    "type": "line",
                    "data": ys,
                }
            ],
            "tooltip": {"trigger": "axis"},
        }


# --------------------------------------------------------------------------- #
# Default impls — pull from rca-agent's AlarmEvent store and InfluxDB adapter.
# Tests inject fakes; production wiring is exercised by integration tests.
# --------------------------------------------------------------------------- #


class RcaAgentAlarmAggregator(AlarmAggregator):
    """Default alarm aggregator: loads ``AlarmEvent`` rows from rca-agent."""

    def __init__(self, rca_store: Any) -> None:
        self._store = rca_store

    def bucket_alarms(
        self, *, metric: str, window_minutes: int, now: datetime
    ) -> list[dict[str, Any]]:
        try:
            alarms = list(getattr(self._store, "alarms", {}).values())
        except Exception:
            return []
        if metric != "alarm_count":
            # Only alarm_count implemented for now.
            return []
        # Bin into 3 equal sub-windows across the window.
        step = max(1, window_minutes // 3)
        bins: dict[int, int] = dict.fromkeys(range(3), 0)
        for alarm in alarms:
            start_str = getattr(alarm, "start_time", None)
            if not start_str:
                continue
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            delta_min = (now - start).total_seconds() / 60.0
            if delta_min < 0 or delta_min > window_minutes:
                continue
            idx = min(2, int(delta_min // step))
            bins[idx] += 1
        return [
            {
                "ts": (now - timedelta(minutes=window_minutes - (i + 1) * step)).isoformat(),
                "count": bins[i],
                "severity": "major",
            }
            for i in range(3)
        ]


class InfluxKpiAggregator(KpiAggregator):
    """Default KPI aggregator: delegates to the InfluxDB adapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def bucket_kpi(
        self, *, metric: str, site_id: str, window_minutes: int, now: datetime
    ) -> list[Any]:
        if self._adapter is None:
            return []
        window = f"{max(1, window_minutes)}m"
        try:
            result = self._adapter.query_kpi(
                metric=metric, site_id=site_id, window=window
            )
        except Exception:
            return []
        return list(getattr(result, "points", []))


from datetime import timedelta  # noqa: E402  (placed here to keep group above tidy)
