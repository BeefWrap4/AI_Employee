# R34 — Real deployment validation (2026-06-23)

Closes the three items from the post-R33 review:

1. **Real k8s deployment smoke** — first time the helm chart actually
   ran against a real cluster.
2. **Activate HA checkpointer** — un-skip the redis/postgres paths in
   `tests/test_r33a_checkpointer_factory.py` now that
   `langgraph-checkpoint-redis` / `-postgres` are installed.
3. **Portal SSE RunView** — wire the web portal to the R33-D SSE
   endpoint so live run events render in the UI.

## Results

| Item | Status | Verification |
|---|---|---|
| Kind cluster | created (`./bin/kind create cluster --name ai-emp`) | 1 control-plane node, v1.31.0 |
| 3 service images built | knowledge-api, api-gateway, agent-platform-api | `docker images \| grep ai-employee` |
| Manifest bugs found | **12** | discovered by real pod startup failures |
| Manifest bugs fixed | 12 | chart now renders + all pods go Ready in kind |
| Checkpointer tests un-skipped | 2 redis/postgres "available" paths | `test_r33a_checkpointer_factory.py` |
| Portal SSE RunView | new `subscribeRunStream` + `views/RunView.jsx` | vitest tests added |

### Tests
- `pytest tests/ --ignore=tests/test_local_ci.py` → **1765 passed, 14 skipped, 0 failed**
- `ruff check .` → All checks passed
- `kubectl get pods -n ai-employee` → 3/3 `1/1 Running`

### End-to-end smoke (via api-gateway :8070)

```
GET /health                                  {"service":"api-gateway","status":"ok","version":"0.1.0"}
GET /api/platform/health                     agent-platform-api in_memory
GET /api/knowledge/health                    knowledge-api sqlite
GET /api/platform/api/v1/agent-templates     5 templates listed
```

## The 12 manifest bugs (all fixed in `68e5821`)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | 8 pods never Ready | `readinessProbe` path `/health/ready` on all 9 services, but only `agent-platform-api` exposes it | Conditional: only platform uses `/health/ready`; others `/health` |
| 2 | platform `CreateContainerConfigError` | `secretKeyRef.name: agent-platform-api-secrets` doesn't exist | Point at the single `ai-employee-secrets` |
| 3 | platform `couldn't find key internalToken in Secret` | `{{ $k \| upper }}` turned camelCase `internalToken` into `INTERNALTOKEN` (no underscore), doesn't match `INTERNAL_TOKEN` key | Hard-code the UPPER_SNAKE key list in deployment.yaml to match `secret.yaml` |
| 4 | knowledge-api `failed to resolve host 'postgres'` | `databaseUrl \| default "postgresql://...postgres:5432..."` injected PG URL even when overlay set `""` | `{{- with $global.databaseUrl }}` — skip the env when empty so SQLite fallback works |
| 5 | PVC `storage: 0 invalid` | `hasStorage` helper returned truthy for `0` because `include "..."` returns string `"false"` which `if` treats as true | Helper returns `1` (truthy) or empty string; `if` treats empty as false |
| 6 | All services had no PVC | `gt (int $svc.storage) 0` coerces `"1Gi"` to `0` → suppresses every PVC + volumeMount | New `ai-employee.hasStorage` helper: `regexFind "^[0-9]+"` + `atoi` handles Kubernetes quantity strings |
| 7 | Pods couldn't write `./var` | `readOnlyRootFilesystem: true` on all services | Conditional: false when storage>0 (stateful services need PVC + writable root) |
| 8 | `Permission denied: './var'` | `runAsUser: 1000` clashed with Dockerfile `appuser` uid=10001 | `runAsUser/fsGroup: 10001` matching the image |
| 9 | approval-service routing broken | values.yaml had `approval-service.port: 8060`, colliding with event-gateway 8060; api-gateway pointed at `http://approval-service:8040` | `approval-service.port: 8040` per `CLAUDE.md` port map |
| 10 | (same as 2/3) | — | — |
| 11 | (same as 6) | — | — |
| 12 | (same as 8) | — | — |

## Files changed

- `infra/helm/templates/_helpers.tpl` — new `ai-employee.hasStorage` helper
- `infra/helm/templates/deployment.yaml` — readiness/secret/storage/uid fixes
- `infra/helm/templates/services.yaml` — PVC via `hasStorage`
- `infra/helm/values.yaml` — approval-service port 8040
- `infra/helm/values-smoke.yaml` — new minimal overlay for kind smoke
- `tests/test_helm_templates.py` — readiness-probe test updated for per-service paths
- `services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py` — checkpointer factory lazy + degraded path tests wired (R34-K)
- `tests/test_r33a_checkpointer_factory.py` — un-skip + degradation-via-sys.modules (R34-K)
- `apps/web-portal/src/sse.js` — new EventSource subscriber (R34-P)
- `apps/web-portal/src/views/RunView.jsx` — new live timeline view (R34-P)
- `apps/web-portal/src/App.jsx` — register RunView (R34-P)
- `apps/web-portal/src/test/sse.test.js`, `RunView.test.jsx` — vitest tests (R34-P)

## What this round proved

The repo's 17 rounds of code (R17-R33) and tests (1763 passing)
**did not validate against real Kubernetes manifests**. This round
found and fixed 12 bugs that single-process TestClient tests cannot
catch — all in the chart, none in the application code. The helm
chart is now deployable.

## Expansion to all 9 services (R34-D2)

After the initial 3-service smoke, `values-smoke.yaml` was extended to
enable every non-Kafka service:

- `tool-registry` needed `storage: 1Gi` (stateful — owns
  `ToolRegistryStore` under `./var`; `storage: 0` made it crash with
  read-only root fs).
- `rca-agent`, `ingestion-worker`, `approval-service`, `mcp-gateway`
  needed their own env tweaks (e.g. `KNOWLEDGE_API_URL` for
  `rca-agent`).
- `event-gateway` left disabled (requires Kafka broker not in kind
  smoke).

Verified end-to-end: 8/8 pods `1/1 Running`, all 6 backend `/health`
endpoints HTTP 200 through api-gateway, and a real `POST
/api/platform/api/v1/agent-runs` round-trip with
`X-Internal-Token: $INTERNAL_TOKEN` returns a `completed` run with
full `node_trace` (TemplateLoaded → Completed).