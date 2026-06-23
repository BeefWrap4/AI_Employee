# ai-employee Helm chart

Helm chart for the AI Employee telecom-ops platform: ten FastAPI services
(knowledge-api, ingestion-worker, rca-agent, agent-platform-api,
tool-registry, approval-service, mcp-gateway, event-gateway, api-gateway)
plus shared Postgres / Redis / object-store wiring.

## Quick start (dev)

```bash
helm install ai-employee . -f values.yaml
```

`values.yaml` is the dev overlay: Postgres default DSN, 1 replica on
knowledge-api (SQLite-safe), 2 replicas elsewhere, HPA only on
agent-platform-api, ingress disabled, auth open. Auth and rate-limit
default to **off** so dev/test traffic flows without tokens.

## Production overlay

`values-prod.yaml` is an overlay meant to be merged on top of the dev
`values.yaml`. Helm coalesces the two files (scalars: prod wins; nested
maps: merged recursively; lists: replaced wholesale), so the per-service
`env`/`pdb`/`port` blocks from `values.yaml` are preserved while the
production knobs override.

```bash
helm install ai-employee . -f values.yaml -f values-prod.yaml
```

What the prod overlay flips vs dev:

- `API_GATEWAY_AUTH_REQUIRED: "true"` — enforce JWT / X-Internal-Token at
  the api-gateway (code default stays `"false"` for dev).
- `RATE_LIMIT_ENABLED: "true"` — engage the sliding-window limiter at the
  api-gateway (code default stays `"false"` for dev).
- `knowledge-api` replicas `1 -> 2` (HA-safe once PG is the default backend).
- `resources.requests`/`limits` on every enabled service
  (agent-platform-api scaled highest at `1500m`/`1Gi`; rca-agent `1000m`/`1Gi`;
  baseline `500m`/`512Mi`).
- HPA enabled on `rca-agent` + `api-gateway` + `mcp-gateway` (in addition
  to the pre-existing `agent-platform-api` HPA from dev).
- `ingress.enabled: true` with TLS (secret `ai-employee-tls`).
- `global.jwtAuthStrict: true` — drop the legacy internal-token path.
- OIDC placeholders (`oidcIssuer`/`oidcClientId`/`oidcAudience`/`oidcJwksUrl`)
  to wire an IdP (Keycloak / Auth0 / Okta).
- `global.storageClassName` placeholder so PVCs bind to a real StorageClass.

Production enforcement is **opt-in via the overlay only** — do not edit
`values.yaml` to flip auth/rate-limit defaults, since dev/test and the
test suite rely on the open defaults.

## Operator overrides

```bash
helm install ai-employee . -f values.yaml -f values-prod.yaml \
  --set global.secrets.oidcIssuer=https://keycloak.example.com/realms/ai-employee \
  --set global.secrets.oidcClientId=ai-employee \
  --set global.storageClassName=gp3 \
  --set global.databaseUrl=postgresql://user:pass@rds-host:5432/ai-employee
```

See `HA.md` for the HA / multi-replica prerequisites (Postgres, Redis,
object store) and `values.yaml` comments for per-service knobs.
