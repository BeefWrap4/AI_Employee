"""event-gateway FastAPI app.

Endpoints:

* ``GET  /health``                       — liveness probe
* ``POST /api/v1/alarms/ingest``         — public HTTP alarm entrypoint
                                            (for non-Kafka sources);
                                            forwards to rca-agent

Lifespan (when ``KAFKA_ENABLED=1``):

* spawns a background task that calls
  :meth:`AlarmForwarder.drain_batch` on a fixed cadence and
  forwards each consumed alarm to the rca-agent via HTTP
* closes the underlying Kafka consumer on shutdown
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from ai_employee.event_gateway.forwarder import (
    AlarmForwarder,
    HttpRcaClient,
    RcaClient,
)
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SERVICE_VERSION = "0.1.0"


class RawAlarmEventIn(BaseModel):
    """Public alarm ingest payload (mirrors rca-agent's RawAlarmEvent)."""

    alarm_id: str
    alarm_code: str
    alarm_name: str
    vendor: str
    site_id: str
    cell_id: str | None = None
    ne_id: str
    severity: str = "major"
    start_time: str
    clear_time: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class AlarmIngestResponse(BaseModel):
    alarm_event_id: str
    fingerprint: str


def _build_default_rca_client() -> RcaClient:
    base = os.getenv("EVENT_GATEWAY_RCA_URL", "")
    if not base:
        raise RuntimeError("EVENT_GATEWAY_RCA_URL is required to forward alarms to the rca-agent")
    return HttpRcaClient(base_url=base)


def create_app(
    *,
    rca_client: RcaClient | None = None,
) -> FastAPI:
    """Build the event-gateway app.

    ``rca_client`` is optional in tests (the HTTP ingest endpoint
    requires a configured client).
    """

    client = rca_client

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # R29-C: the Kafka consumer lives here (not in rca-agent).
        # The forwarder drains messages and POSTs each to the rca-agent
        # over HTTP.  When Kafka is disabled (or build fails) the
        # service still serves ``/health`` + ``/api/v1/alarms/ingest``.
        from ai_employee.rca_agent.kafka_ingest import build_alarm_consumer

        consumer = build_alarm_consumer()
        if consumer is not None:
            forwarder = AlarmForwarder(
                rca_client=client or _build_default_rca_client(),
            )
            task: asyncio.Task[None] | None = asyncio.create_task(_poll_loop(consumer, forwarder))
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # pragma: no cover
                        pass
                try:
                    consumer.close()
                except Exception:  # pragma: no cover
                    pass
        else:
            yield

    app = FastAPI(
        title="AI Employee Event Gateway",
        version=SERVICE_VERSION,
        lifespan=_lifespan,
    )
    # R25-L: shared rate-limit middleware (no-op unless RATE_LIMIT_ENABLED=true).
    from ai_employee.rate_limit import install_rate_limiter

    install_rate_limiter(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "service": "event-gateway",
            "status": "ok",
            "version": SERVICE_VERSION,
        }

    @app.post(
        "/api/v1/alarms/ingest",
        response_model=AlarmIngestResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def ingest_alarm(payload: RawAlarmEventIn) -> AlarmIngestResponse:
        # Resolve the rca-client lazily so the endpoint is callable
        # even when no client was injected and the env has been set
        # by the lifespan config.
        nonlocal client
        if client is None:
            client = _build_default_rca_client()
        try:
            resp = client.post_alarm(payload.model_dump())
        except Exception as exc:
            logger.warning("event-gateway forward to rca-agent failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "rca_agent_forward_failed",
                    "message": str(exc),
                },
            ) from exc
        return AlarmIngestResponse(
            alarm_event_id=resp.get("alarm_event_id", ""),
            fingerprint=resp.get("fingerprint", ""),
        )

    return app


async def _poll_loop(consumer: Any, forwarder: AlarmForwarder) -> None:
    """Background task: drain Kafka → forward HTTP → loop.

    Cadence (0.5 s sleep between empty polls) mirrors the pre-R29-C
    rca-agent loop so the alarm-pipeline latency is unchanged.
    """
    while True:
        try:
            forwarder.drain_batch(kafka_consumer=consumer, max_messages=100)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("event-gateway poll loop error: %s", exc)
        await asyncio.sleep(0.5)


# Re-export the kafka ingest entrypoint for tests that monkeypatch
# ``ai_employee.event_gateway.app.build_alarm_consumer``.
def __getattr__(name: str) -> Any:
    if name == "build_alarm_consumer":
        from ai_employee.rca_agent.kafka_ingest import (
            build_alarm_consumer as _bac,
        )

        return _bac
    raise AttributeError(name)


app = create_app()
