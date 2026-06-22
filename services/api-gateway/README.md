# api-gateway

Single ingress-level API gateway (spec §三 §5.1).  Owns
authentication, rate limiting, audit, routing, and trace_id + run_id
propagation so the six backend services stay independently deployable
but are reached through one front door.  R32-A.

## Routing

Path prefix → backend:

| Prefix | Backend | Port |
| --- | --- | --- |
| `/api/knowledge/*`  | knowledge-api        | 8010 |
| `/api/rca/*`        | rca-agent            | 8020 |
| `/api/platform/*`   | agent-platform-api   | 8030 |
| `/api/tools/*`      | tool-registry        | 8040 |
| `/api/approvals/*`  | approval-service     | 8040 |
| `/api/mcp/*`        | mcp-gateway          | 8050 |

The matched prefix is stripped so each backend sees its own natural
path (e.g. `/api/knowledge/v1/docs` → knowledge-api + `/v1/docs`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/health`           | liveness probe (exempt from auth + audit) |
| any  | `/api/{svc}/*`      | routed + forwarded to the matching backend |

## Cross-cutting concerns

- **Auth** — `API_GATEWAY_AUTH_REQUIRED=true` (default `false`, open)
  gates on the shared `auth-policy` primitives: HS256 JWT `Bearer`
  first, then `X-Internal-Token`.  401 on missing/invalid credentials.
- **Rate limit** — `install_rate_limiter` from the shared `rate-limit`
  package (no-op unless `RATE_LIMIT_ENABLED=true`); 429 when exceeded.
- **trace_id** — mints a UUID when the caller sends no `X-Trace-Id` and
  always propagates the resolved id to the backend + the response.
- **run_id** — an `X-Run-Id` header is forwarded to the backend
  unchanged (lets a caller correlate a gateway request with a run).
- **Audit** — every request (forwarded or rejected) appends a record to
  `app.state.audit_log` with `trace_id` / `run_id` / method / path /
  backend / status / timestamp.

## Env

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_GATEWAY_AUTH_REQUIRED` | `false` | `true` → enforce JWT/internal-token on every routed call |
| `API_GATEWAY_<BACKEND>_URL` | in-cluster DNS | per-backend upstream URL override |
| `RATE_LIMIT_ENABLED` | `false` | `true` → mount the shared sliding-window limiter |
| `RATE_LIMIT_LIMIT` / `RATE_LIMIT_WINDOW_SECONDS` | `60` / `60` | limiter knobs |
| `RATE_LIMIT_KEY_FUNC` | `user` | throttle dimension (`user`/`tenant`/`endpoint`/`tool`) |
| `JWT_SECRET` / `INTERNAL_TOKEN` | — | shared auth secrets (see `auth-policy`) |

## Run

```bash
docker build -f services/api-gateway/Dockerfile -t ai-employee/api-gateway .
docker run -p 8070:8070 \
  -e API_GATEWAY_AUTH_REQUIRED=false \
  -e API_GATEWAY_KNOWLEDGE_API_URL=http://knowledge-api:8010 \
  -e API_GATEWAY_RCA_AGENT_URL=http://rca-agent:8020 \
  ai-employee/api-gateway
```
