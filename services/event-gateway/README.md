# event-gateway

Standalone Kafka→HTTP alarm forwarder (spec §9 deployable unit
`event-gateway`).  Owns the Kafka alarm subscription that used to
live in the rca-agent's lifespan (R27).  Drains messages from the
configured alarm topic and forwards each alarm via HTTP POST to the
rca-agent's `POST /api/v1/alarms/events` endpoint.

Also exposes a public `POST /api/v1/alarms/ingest` endpoint for
non-Kafka alarm sources (south-bound NMS adapters, replay tooling,
etc.) so all alarm traffic flows through this gateway.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/health`                    | liveness probe |
| POST | `/api/v1/alarms/ingest`      | public alarm ingest → forwards to rca-agent |

## Why a separate service

Before R29-C the rca-agent process hosted the Kafka consumer in its
lifespan.  When rca-agent restarted, the alarm stream paused.  R29-C
extracts the consumer into this gateway so:

- rca-agent stays a pure HTTP consumer of `/api/v1/alarms/events` and
  is Kafka-unaware.
- The alarm stream survives rca-agent restarts.
- The consumer can be scaled independently (the gateway is stateless
  from rca-agent's perspective; the Kafka consumer group handles
  partition rebalancing).

## Env

| Variable | Default | Purpose |
| --- | --- | --- |
| `KAFKA_ENABLED` | unset | truthy → spawn Kafka poll task in lifespan |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | broker list |
| `KAFKA_ALARM_TOPIC` | `alarms` | topic to subscribe |
| `KAFKA_GROUP_ID` | `event-gateway` | consumer group |
| `EVENT_GATEWAY_RCA_URL` | — | rca-agent base URL (`http://rca-agent:8020` in k8s) |
| `INTERNAL_TOKEN` | unset | forwarded as `X-Internal-Token` to rca-agent |

## Run

```bash
docker build -f services/event-gateway/Dockerfile -t ai-employee/event-gateway .
docker run -p 8060:8060 \
  -e KAFKA_ENABLED=1 \
  -e KAFKA_BOOTSTRAP_SERVERS=kafka:9092 \
  -e EVENT_GATEWAY_RCA_URL=http://rca-agent:8020 \
  -e INTERNAL_TOKEN=change-me \
  ai-employee/event-gateway
```
