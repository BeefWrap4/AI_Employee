"""RCA tool adapters — pluggable data-source adapters for evidence collection.

Each adapter implements ``fetch(incident) -> list[Evidence]``.  The default
implementation is deterministic fixture data so the agent runs without a
live Prometheus / Elasticsearch / Neo4j / ticketing system.  Real adapters
are gated by env vars (``PROMETHEUS_ENABLED`` etc.) and selected by
:func:`build_adapters`.

Adapters are intentionally read-only.  They translate their backing store
into the platform-wide ``Evidence`` schema so the runtime does not care
whether the data came from a fixture or a real system.

R25-T.4: the ``_HttpAdapter._get`` method is now wrapped with
``resilient_fetch`` from ``http_resilience``, which adds a hard
thread-based timeout and configurable retry (env:
``RL_HTTP_RETRY_MAX_ATTEMPTS``, ``RL_HTTP_RETRY_BACKOFF_SECONDS``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from ai_employee.rca_agent.http_resilience import resilient_fetch  # R25-T.4
from ai_employee.rca_agent.schemas import Evidence, IncidentResponse


class ToolAdapter(Protocol):
    """Minimal contract for an evidence source adapter."""

    name: str
    source_type: str

    def fetch(self, incident: IncidentResponse) -> list[Evidence]: ...


# --------------------------------------------------------------------------- #
# Fixture adapters (deterministic, no external dependency)
# --------------------------------------------------------------------------- #


class FixtureKPIAdapter:
    name = "fixture.kpi"
    source_type = "metric"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        return [
            Evidence(
                evidence_id=f"kpi_{primary.site_id}_rrc",
                source_type="metric",
                source_ref=f"kpi:{primary.site_id}:{primary.cell_id or 'site'}:rrc_setup_fail",
                content=(
                    f"RRC setup failure rate and transport error counters elevated "
                    f"for {primary.site_id} in the alarm window."
                ),
                confidence=0.82,
            ),
        ]


class FixtureLogAdapter:
    name = "fixture.log"
    source_type = "log"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        return [
            Evidence(
                evidence_id=f"log_{primary.ne_id}",
                source_type="log",
                source_ref=f"log:{primary.ne_id}",
                content=(
                    f"NE logs at {primary.ne_id} contain {primary.alarm_code} "
                    f"near the incident start time."
                ),
                confidence=0.76,
            ),
        ]


class FixtureTopologyAdapter:
    name = "fixture.topology"
    source_type = "topology"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        return [
            Evidence(
                evidence_id=f"topo_{primary.site_id}",
                source_type="topology",
                source_ref=f"topology:{primary.site_id}",
                content=(
                    f"Affected cell on {primary.site_id} shares the upstream "
                    f"transmission path with the primary alarm source."
                ),
                confidence=0.78,
            ),
        ]


class FixtureTicketAdapter:
    name = "fixture.ticket"
    source_type = "ticket"

    def __init__(self, fixture_path: str | None = None) -> None:
        self._fixture_path = fixture_path or os.getenv("TICKET_API_FIXTURE_PATH")

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        history = self._load_history(primary.site_id)
        if history:
            return [
                Evidence(
                    evidence_id=f"ticket_history_{primary.site_id}",
                    source_type="ticket",
                    source_ref=f"ticket-history:{primary.site_id}",
                    content=(
                        f"Similar historical cases at {primary.site_id} closed as: "
                        f"{'; '.join(history)}."
                    ),
                    confidence=0.66,
                )
            ]
        return [
            Evidence(
                evidence_id=f"ticket_history_{primary.site_id}",
                source_type="ticket",
                source_ref=f"ticket-history:{primary.site_id}",
                content=(
                    f"No prior ticket history at {primary.site_id} for "
                    f"{primary.alarm_code}; defaulting to general transmission advice."
                ),
                confidence=0.5,
            )
        ]

    def _load_history(self, site_id: str) -> list[str]:
        path = self._fixture_path
        if not path:
            return []
        p = Path(path)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            entries = data.get(site_id, [])
        elif isinstance(data, list):
            entries = data
        else:
            return []
        return [str(x) for x in entries]


# --------------------------------------------------------------------------- #
# Real adapters (HTTP-based, fail-open with structured errors)
# --------------------------------------------------------------------------- #


class _HttpAdapter:
    """Minimal HTTP wrapper with timeout + structured error.

    R25-T.4: ``_get`` delegates to :func:`resilient_fetch` so each
    adapter call gets a hard timeout (thread-based) + configurable
    retry (env: ``RL_HTTP_RETRY_MAX_ATTEMPTS``,
    ``RL_HTTP_RETRY_BACKOFF_SECONDS``).
    """

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        def _request() -> Any:
            try:
                resp = httpx.get(
                    f"{self.base_url}{path}",
                    params=params or {},
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                raise AdapterUnavailable(f"{self.base_url} unreachable: {exc}") from exc
            if resp.status_code >= 500:
                raise AdapterUnavailable(f"{self.base_url} returned {resp.status_code}")
            if resp.status_code >= 400:
                raise AdapterBadRequest(resp.status_code, resp.text)
            return resp.json()

        from ai_employee.rca_agent.http_resilience import _FetchTimeoutError, resilient_fetch

        try:
            return resilient_fetch(
                _request,
                timeout_ms=int(self._timeout * 1000),
            )
        except _FetchTimeoutError as exc:
            raise AdapterUnavailable(
                f"{self.base_url} timeout after {self._timeout}s: {exc}"
            ) from exc


class AdapterUnavailable(Exception):
    """Adapter's backing service is unreachable or unhealthy."""


class AdapterBadRequest(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"bad request: {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class PrometheusKPIAdapter(_HttpAdapter):
    name = "prometheus.kpi"
    source_type = "metric"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        data = self._get(
            "/api/v1/query",
            params={"query": f'up{{site="{primary.site_id}"}}'},
        )
        result_count = len((data or {}).get("data", {}).get("result", []) or [])
        return [
            Evidence(
                evidence_id=f"prom_{primary.site_id}",
                source_type="metric",
                source_ref=f"prometheus:{primary.site_id}",
                content=(
                    f"Prometheus reported {result_count} active series for site {primary.site_id}."
                ),
                confidence=0.6 if result_count else 0.3,
            )
        ]


class ElasticsearchLogAdapter(_HttpAdapter):
    name = "elasticsearch.log"
    source_type = "log"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        data = self._get(
            f"/{primary.ne_id}/_search",
            params={"q": primary.alarm_code, "size": 1},
        )
        hits = (data or {}).get("hits", {}).get("total", {})
        count = hits.get("value", 0) if isinstance(hits, dict) else int(hits or 0)
        return [
            Evidence(
                evidence_id=f"es_{primary.ne_id}",
                source_type="log",
                source_ref=f"elasticsearch:{primary.ne_id}",
                content=(
                    f"Elasticsearch reports {count} log hits for alarm "
                    f"{primary.alarm_code} on {primary.ne_id}."
                ),
                confidence=0.65 if count else 0.3,
            )
        ]


class Neo4jTopologyAdapter(_HttpAdapter):
    name = "neo4j.topology"
    source_type = "topology"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        data = self._get(
            "/topology/dependencies",
            params={"site_id": primary.site_id},
        )
        deps = (data or {}).get("upstream", []) if isinstance(data, dict) else []
        return [
            Evidence(
                evidence_id=f"neo4j_{primary.site_id}",
                source_type="topology",
                source_ref=f"neo4j:{primary.site_id}",
                content=(
                    f"Topology graph reports {len(deps)} upstream dependencies "
                    f"for {primary.site_id}."
                ),
                confidence=0.7 if deps else 0.4,
            )
        ]


class TicketApiAdapter(_HttpAdapter):
    name = "ticket_api.ticket"
    source_type = "ticket"

    def fetch(self, incident: IncidentResponse) -> list[Evidence]:
        primary = incident.primary_alarm
        data = self._get(
            "/tickets/history",
            params={"site_id": primary.site_id, "limit": 5},
        )
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        summary = "; ".join(t.get("summary", "") for t in items[:3]) or "no history"
        return [
            Evidence(
                evidence_id=f"ticket_api_{primary.site_id}",
                source_type="ticket",
                source_ref=f"ticket-api:{primary.site_id}",
                content=f"Recent tickets for {primary.site_id}: {summary}",
                confidence=0.6 if items else 0.3,
            )
        ]


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def build_adapters() -> dict[str, ToolAdapter]:
    """Build the four evidence source adapters based on env flags.

    Returns a dict with keys ``kpi``, ``log``, ``topology``, ``ticket``.
    When an env flag is not set or the URL is not configured, the
    corresponding fixture adapter is returned.
    """
    kpi: ToolAdapter
    if _truthy(os.getenv("PROMETHEUS_ENABLED")) and os.getenv("PROMETHEUS_URL"):
        kpi = PrometheusKPIAdapter(os.getenv("PROMETHEUS_URL", ""))
    else:
        kpi = FixtureKPIAdapter()

    log: ToolAdapter
    if _truthy(os.getenv("ELASTICSEARCH_ENABLED")) and os.getenv("ELASTICSEARCH_URL"):
        log = ElasticsearchLogAdapter(os.getenv("ELASTICSEARCH_URL", ""))
    else:
        log = FixtureLogAdapter()

    topo: ToolAdapter
    if _truthy(os.getenv("NEO4J_ENABLED")) and os.getenv("NEO4J_URL"):
        topo = Neo4jTopologyAdapter(os.getenv("NEO4J_URL", ""))
    else:
        topo = FixtureTopologyAdapter()

    ticket: ToolAdapter
    if _truthy(os.getenv("TICKET_API_ENABLED")) and os.getenv("TICKET_API_URL"):
        ticket = TicketApiAdapter(os.getenv("TICKET_API_URL", ""))
    else:
        ticket = FixtureTicketAdapter()

    return {"kpi": kpi, "log": log, "topology": topo, "ticket": ticket}


__all__ = [
    "AdapterBadRequest",
    "AdapterUnavailable",
    "ElasticsearchLogAdapter",
    "FixtureKPIAdapter",
    "FixtureLogAdapter",
    "FixtureTicketAdapter",
    "FixtureTopologyAdapter",
    "Neo4jTopologyAdapter",
    "PrometheusKPIAdapter",
    "TicketApiAdapter",
    "ToolAdapter",
    "build_adapters",
    "resilient_fetch",  # R25-T.4 re-export
]
