# R23 — High Availability & Multi-Replica Deployment

This document describes how to run the AI Employee services with more
than one replica behind a load balancer, and the prerequisites that
must be in place before scaling horizontally.

## TL;DR

Multi-replica HA is **opt-in**. The default chart ships services at
1–2 replicas with SQLite/LocalFs backends; scaling any service beyond
one replica requires externalising its state to Postgres + Redis +
object storage.

| Service            | Default replicas | HA-safe replicas | State backend required           |
| ------------------ | ---------------: | ---------------: | -------------------------------- |
| agent-platform-api | 2                | 2+               | Redis (lease, idempotency, bus) + APPROVAL_SERVICE_URL + MCP_GATEWAY_URL |
| approval-service   | 2                | 2+               | Postgres (approval_tasks)         |
| mcp-gateway        | 2                | 2+               | Postgres or Redis (tool registry) |
| tool-registry      | 2                | 2+               | MCP_GATEWAY_URL (delegated)       |
| rca-agent          | 2                | 2+               | none (read-only over external APIs) |
| ingestion-worker   | 2                | 2+               | shared embedding provider / object store |
| knowledge-api      | 1                | 1 (until PG)     | Postgres + object store (uploads) |

## Prerequisites

Before raising any replica count above 1, all of the following must be
provisioned and reachable from every pod:

1. **Postgres** — the shared relational store. Services that currently
   default to a per-pod SQLite file (`KNOWLEDGE_SQLITE_PATH`,
   `approval-service` on-disk store) must switch to
   `PLATFORM_DB_BACKEND=postgres` so two pods see the same rows.
2. **Redis** — used by four HA subsystems:
   - **Leader election** (`build_leader_election`) — only the replica
     holding the `leader:agent-platform:scheduler` lease ticks the cron
     scheduler, so N replicas don't each fire every due schedule.
   - **Rate limiting** (`rate_limit_redis.RedisBackend`) — a single
     sliding-window bucket shared across replicas.
   - **Idempotency store** (`RedisIdempotencyStore`, R23-1) — so a
     retried request replayed to a different replica returns the
     cached result instead of re-executing.
   - **Event bus** (`RedisEventBus`, R23-3) — run events published on
     replica A are delivered to WebSocket subscribers on replica B via
     Redis pub/sub.
3. **Object storage** (S3 / MinIO, R22) — document uploads and
   supplement attachments must not live on a per-pod PVC when more
   than one pod can receive an upload. Set `OBJECT_STORE_URL`.

## Enabling multi-replica mode

Set the env that activates the HA backends on `agent-platform-api`:

```yaml
env:
  REDIS_URL: redis://redis:6379/0
  EVENT_BUS_BACKEND: redis          # R23-3: mirror run events on Redis pub/sub
  APPROVAL_SERVICE_URL: http://approval-service:8060   # R21
  MCP_GATEWAY_URL: http://mcp-gateway:8050             # R21
  OBJECT_STORE_URL: http://minio:9000                   # R22
  SQLITE_PATH: ""                   # readiness probe skips sqlite when unset
```

The chart already sets `EVENT_BUS_BACKEND: redis` for
`agent-platform-api` in `values.yaml`. Provide `REDIS_URL` via the
secret / configmap for your environment.

## How each subsystem stays correct under N replicas

### Scheduler (cron) — leader election

`SchedulerLoop._ensure_leader` acquires / renews a Redis lease before
each tick. Non-leader replicas sleep through the interval and re-attempt
acquisition, so a crashed leader is succeeded within one tick window
(`tick_interval_s`, default 30s; lease TTL 15s). On graceful shutdown
the leader releases the lease so a standby takes over immediately.

### API side effects — idempotency

Every mutating POST that can be retried by a client or replayed by a
load balancer accepts an `Idempotency-Key` header (R23-2):

- `POST /api/v1/agent-runs`
- `POST /api/v1/evaluations/runs`
- `POST /api/v1/documents` (key = `Idempotency-Key` + `sha256(content)`)

The first request claims the key `in_flight`, executes, and stores the
response (`success` / `failed`). A replay hits the cached record and
returns the original body verbatim — no duplicate run / doc / eval.
The Redis backend makes the cache visible to all replicas.

### Live updates — Redis event bus

`/api/v1/ws/runs/{run_id}` subscribes to the in-process `EventBus`
singleton. Under `EVENT_BUS_BACKEND=redis`, `RedisEventBus` wraps that
singleton: `publish` pushes to local queues AND mirrors onto a Redis
channel; a per-replica listener fans received messages back into the
local bus. A WebSocket client connected to replica B therefore
receives events published on replica A. Redis outages degrade
gracefully to local-only delivery.

### Rate limiting

`SlidingWindowLimiter` with `RedisBackend` shares one timestamp log
per key across replicas, so a burst split across two pods still counts
against the single budget.

## Health probes & graceful shutdown

- `/health` (liveness) — cheap, always 200 unless the process is
  shutting down. k8s restarts the pod on failure.
- `/health/ready` (readiness) — probes configured downstream deps
  (`SQLITE_PATH`, `REDIS_URL`). Returns 503 when any dep is unhealthy
  so k8s stops routing traffic without restarting the pod.

Both probes are wired in `infra/helm/templates/deployment.yaml`. The
uvicorn entrypoint should be launched with `--timeout-graceful-shutdown`
(or a SIGTERM handler) so in-flight requests drain before the pod exits;
the `RedisEventBus` listener and scheduler lease are released on
shutdown via the FastAPI lifespan.

## Failover behaviour

When the leader replica crashes:

1. Its Redis lease expires after `ttl_s` (15s).
2. A standby replica's next `try_acquire()` succeeds.
3. The new leader ticks the scheduler; the old leader's in-flight tick
   (if any) had already completed or is abandoned.

Because the scheduler store (`ScheduledRunStore`) and run store
(`AgentRunStore`) are Postgres-backed in HA mode, the new leader sees
the same due schedules and run records — no double-tick, no lost tick.

See `tests/test_ha_leader_failover.py` for the regression test that
simulates this handoff with a fake Redis lease.
