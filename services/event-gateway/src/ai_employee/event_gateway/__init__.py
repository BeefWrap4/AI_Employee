"""event-gateway: independent Kafka→HTTP alarm forwarder (spec §9).

Owns the Kafka alarm subscription that used to live in the rca-agent
lifespan (R27).  Drains messages and forwards them via HTTP POST to
the rca-agent's ``/api/v1/alarms/events`` endpoint.  Exposes an HTTP
ingest path (``/api/v1/alarms/ingest``) so non-Kafka alarm sources can
reach the rca-agent through the same gateway.

Env:

* ``KAFKA_ENABLED``        — when truthy, lifespan spawns a Kafka poll task.
* ``KAFKA_BOOTSTRAP_SERVERS``, ``KAFKA_ALARM_TOPIC``, ``KAFKA_GROUP_ID``.
* ``EVENT_GATEWAY_RCA_URL`` — rca-agent base URL (required when forwarding).
* ``INTERNAL_TOKEN``       — forwarded as ``X-Internal-Token`` on the
                              cross-service POST so rca-agent's
                              ``require_oidc_or_internal`` accepts it.

Note: we deliberately do NOT do ``from .app import app`` here.  The
``app`` symbol would shadow the ``app`` submodule on the package
namespace, making ``import ai_employee.event_gateway.app as foo``
resolve to the FastAPI instance rather than the module.  Tests +
deploy entry points import the FastAPI app directly via
``ai_employee.event_gateway.app:app`` (see Dockerfile ``APP_MODULE``).
"""

from __future__ import annotations

from ai_employee.event_gateway.forwarder import (
    AlarmForwarder,
    HttpRcaClient,
    RcaClient,
)

__all__ = [
    "AlarmForwarder",
    "HttpRcaClient",
    "RcaClient",
]
