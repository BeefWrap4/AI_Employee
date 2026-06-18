"""InfluxDB time-series KPI query (spec P2 §4 InfluxDB/企业时序库).

Queries PRB utilization / RRC failure / KPI metrics from InfluxDB 2.x.
Sits alongside the existing :class:`PrometheusKPIAdapter`; the active
backend is selected via env (``KPI_BACKEND=prometheus|influx``).

The InfluxDB client is pluggable behind :class:`InfluxClientProtocol`;
tests inject :class:`FakeInfluxClient` so no live database is required.
:func:`build_influx_kpi_adapter` returns ``None`` when InfluxDB is
unset/unreachable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class KpiPoint:
    """One KPI sample."""

    ts: str
    value: float
    field: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KpiQueryResult:
    """Aggregated KPI query result for one (metric, site) pair."""

    metric: str
    site_id: str
    points: list[KpiPoint] = field(default_factory=list)

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def avg(self) -> float | None:
        return sum(p.value for p in self.points) / len(self.points) if self.points else None

    @property
    def max(self) -> float | None:
        return max((p.value for p in self.points), default=None)

    @property
    def min(self) -> float | None:
        return min((p.value for p in self.points), default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "site_id": self.site_id,
            "point_count": self.point_count,
            "avg": self.avg,
            "min": self.min,
            "max": self.max,
            "points": [p.to_dict() for p in self.points],
        }


# --------------------------------------------------------------------------- #
# Client protocol + fake
# --------------------------------------------------------------------------- #


class InfluxClientProtocol(Protocol):
    def query(self, *, metric: str, site_id: str, window: str) -> list[KpiPoint]: ...
    def close(self) -> None: ...


class FakeInfluxClient:
    """In-memory client; ``seed`` populates (site, metric) → points."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], list[KpiPoint]] = {}

    def seed(self, site_id: str, metric: str, points: list[KpiPoint]) -> None:
        self._store[(site_id, metric)] = list(points)

    def query(self, *, metric: str, site_id: str, window: str) -> list[KpiPoint]:
        return list(self._store.get((site_id, metric), []))

    def close(self) -> None:
        self._store.clear()


# --------------------------------------------------------------------------- #
# InfluxKpiAdapter
# --------------------------------------------------------------------------- #


class InfluxKpiAdapter:
    """Queries KPI metrics from InfluxDB 2.x (Flux)."""

    def __init__(self, *, client: InfluxClientProtocol, bucket: str = "kpi") -> None:
        self._client = client
        self._bucket = bucket

    def query_kpi(self, *, metric: str, site_id: str, window: str = "1h") -> KpiQueryResult:
        points = self._client.query(metric=metric, site_id=site_id, window=window)
        return KpiQueryResult(metric=metric, site_id=site_id, points=points)

    def query_metrics(
        self, *, metrics: list[str], site_id: str, window: str = "1h",
    ) -> list[KpiQueryResult]:
        return [
            self.query_kpi(metric=m, site_id=site_id, window=window)
            for m in metrics
        ]

    def to_evidence_payload(self, result: KpiQueryResult) -> dict[str, Any]:
        avg = result.avg
        content = (
            f"InfluxDB KPI {result.metric} for {result.site_id}: "
            f"{result.point_count} samples, avg={avg}, max={result.max}."
        )
        return {
            "evidence_id": f"influx_{result.site_id}_{result.metric}",
            "source_type": "metric",
            "source_ref": f"influxdb:{result.site_id}:{result.metric}",
            "content": content,
            "confidence": 0.65 if result.point_count else 0.3,
            "stats": {
                "avg": avg,
                "min": result.min,
                "max": result.max,
                "point_count": result.point_count,
            },
        }

    def close(self) -> None:
        self._client.close()


def _connect_influx(
    *, url: str, token: str, org: str, timeout_s: float,
) -> InfluxClientProtocol:
    from influxdb_client import InfluxDBClient  # type: ignore[import-not-found]
    from influxdb_client.client.flux_table import FluxTable

    client = InfluxDBClient(url=url, token=token, org=org, timeout=int(timeout_s * 1000))

    class _SyncAdapter:
        def __init__(self, influx_client):
            self._client = influx_client

        def query(self, *, metric: str, site_id: str, window: str) -> list[KpiPoint]:
            flux = (
                f'from(bucket: "{os.environ.get("INFLUXDB_BUCKET", "kpi")}") '
                f"|> range(start: -{window}) "
                f'|> filter(fn: (r) => r._measurement == "{metric}") '
                f'|> filter(fn: (r) => r.site_id == "{site_id}")'
            )
            tables = self._client.query_api().query(flux)
            points: list[KpiPoint] = []
            for table in tables or []:
                for record in table.records:
                    points.append(KpiPoint(
                        ts=str(record.get_time()),
                        value=float(record.get_value()),
                        field=str(record.get_field()),
                    ))
            return points

        def close(self) -> None:
            self._client.close()

    return _SyncAdapter(client)


def build_influx_kpi_adapter() -> InfluxKpiAdapter | None:
    """Build an adapter from env.  Returns ``None`` when InfluxDB unset/unreachable.

    Env: ``INFLUXDB_URL``, ``INFLUXDB_TOKEN``, ``INFLUXDB_ORG``,
    ``INFLUXDB_BUCKET``, ``INFLUXDB_TIMEOUT_S``.
    """
    url = os.environ.get("INFLUXDB_URL")
    if not url:
        return None
    token = os.environ.get("INFLUXDB_TOKEN", "")
    org = os.environ.get("INFLUXDB_ORG", "ai-employee")
    bucket = os.environ.get("INFLUXDB_BUCKET", "kpi")
    try:
        timeout = float(os.environ.get("INFLUXDB_TIMEOUT_S", "2.0"))
        client = _connect_influx(url=url, token=token, org=org, timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("InfluxDB unavailable (%s): %s", url, exc)
        return None
    return InfluxKpiAdapter(client=client, bucket=bucket)


__all__ = [
    "FakeInfluxClient",
    "InfluxClientProtocol",
    "InfluxKpiAdapter",
    "KpiPoint",
    "KpiQueryResult",
    "build_influx_kpi_adapter",
]