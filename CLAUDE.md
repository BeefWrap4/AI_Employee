# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository shape

This is the AI Employee monorepo: three progressive telecom-ops AI projects on a single backbone, plus an agent-platform over them.

| Layer | Path | Purpose |
|---|---|---|
| Specs | `Docs/project-1-*.md`, `project-2-*.md`, `project-3-*.md` | Authoritative design specs for each project |
| Implementation plan | `Docs/ai-agent-telecom-projects-implementation-plan.md` | Roadmap + phased deliverables |
| Gap analysis | `Docs/gap-analysis-2026-06-18.md` | Single-document audit of current code vs spec (P0–P3 buckets) |
| Closure specs | `Docs/superpowers/specs/2026-06-19-r{19,22..27}-*.md` | Per-round design notes for recent gap-closure work |
| Source | `services/<name>/src/ai_employee/<name>/` | One FastAPI app per service |
| Shared packages | `packages/<name>/src/ai_employee/<name>/` | Cross-service libs (no FastAPI app) |
| Web portal | `apps/web-portal/` | Vite + React SPA (nginx-served) |
| Migrations | `migrations/versions/00*.py` | Alembic |
| Deploy | `infra/{k8s,helm,docker-compose}/` | K8s manifests + Helm chart + dev compose |
| Observability | `infra/observability/` | Prometheus scrape config + Grafana provisioning (R33-G1) |

### Services

`services/` houses ten FastAPI apps; `packages/` holds libs that multiple services import. Service ports follow the convention `8010–8070` (10-step), allocated in the order they were extracted. `api-gateway` (8070) is the single ingress-level front door for user traffic (R32-A).

```
services/knowledge-api          (8010)  RAG knowledge base (project-1)
services/ingestion-worker       (8011)  PDF/DOCX/XLSX/MD parsing + embedding
services/rca-agent              (8020)  Alarm RCA pipeline (project-2)
services/agent-platform-api     (8030)  Agent runtime, approvals, tools (project-3)
services/tool-registry          (8040)  MCP tool registry
services/approval-service       (8040)  Standalone approval task service (post-R21)
services/mcp-gateway            (8050)  MCP protocol gateway (post-R21)
services/event-gateway          (8060)  Kafka alarm consumer + event-ingest (post-R29-C)
services/api-gateway            (8070)  Single ingress gateway — routing + auth + ratelimit + trace + audit (R32-A)
services/eval-service           (— CLI)  Eval center CLI runner (no HTTP)
apps/web-portal                 (80)    Vite SPA
packages/common-schemas                       Pydantic models, idempotency, metrics bridge, redaction, knowledge types
packages/llm-gateway                          OpenAI-compatible chat/embed client, model registry
packages/observability                       Langfuse emitter, Prometheus text renderer, metric registry
packages/auth-policy                          OIDC verify, JWT, RBAC, FastAPI deps
packages/object-store                        LocalFs + S3/MinIO object-store Protocol
packages/rate-limit                          Sliding-window limiter + FastAPI middleware (shared by 6 services)
```

### Inter-service contracts

Each "split-out" service (post-R21/R22/R23) supports two backends via env flag, picked in `app.py`'s `create_app`:

- `APPROVAL_SERVICE_URL` unset → `InMemoryApprovalServiceClient` (default for tests; binds to platform's in-process store)
- `APPROVAL_SERVICE_URL` set → `HttpApprovalServiceClient` (delegates to `services/approval-service`)

Same pattern for `MCP_GATEWAY_URL` and the in-process → HTTP switch. Use the same shape (`Protocol` + `InMemory*` + `Http*` + `build_*_client()` factory) when adding new externalized services. The clients live in `services/agent-platform-api/src/ai_employee/agent_platform_api/clients.py`.

**Distributed trace propagation (R33-H):** `bind_trace_context(trace_id, run_id=None)` (in `clients.py`) is a contextvar-based context manager. The platform's `trace_context_middleware` binds it from the inbound `X-Trace-Id`/`X-Run-Id` (mints a trace_id when absent), and the `Http*Client._headers()` calls inject those headers on outbound calls **only when a context is active** — so existing tests that assert exact header sets still pass. The api-gateway already mints/propagates `X-Trace-Id` to backends, so the full chain is api-gateway → platform → approval-service/mcp-gateway.

**SSE streaming (R33-D):** `GET /api/v1/agent-runs/{run_id}/stream` on agent-platform-api returns `text/event-stream`, replaying the last 50 `RunEvent`s from `platform_bus` then streaming live ones as `data: {json}\n\n`. The portal's `apps/web-portal/src/sse.js` (`subscribeRunStream`) + `views/RunView.jsx` consume it via native `EventSource`.

## Common commands

```bash
# Setup (Miniconda is the canonical env)
conda env create -f environment.yml
conda activate ai-employee
pip install -e ".[dev]"

# Run full test suite (all packages)
pytest                                  # ~1815 tests, ~2 min
pytest tests/ --ignore=tests/test_local_ci.py   # the canonical CI invocation (skips the recursive local-ci suite)

# Run a single test or subset
pytest tests/test_r25_observability_metrics.py
pytest tests/test_r25_observability_metrics.py::test_prometheus_text_contains_seven_indicators
pytest tests/ --ignore=tests/test_local_ci.py -k "RCA"   # all RCA-related tests

# Lint + format (ruff is the only linter; mypy is configured but not in CI)
ruff check .
ruff format .

# Run a single service
uvicorn ai_employee.knowledge_api.app:app --port 8010 --app-dir services/knowledge-api/src
uvicorn ai_employee.rca_agent.app:app --port 8020 --app-dir services/rca-agent/src
uvicorn ai_employee.agent_platform_api.app:app --port 8030 --app-dir services/agent-platform-api/src

# RCA replay eval (sample cases file in tests/rca-replay/)
python -m ai_employee.rca_agent.replay tests/rca-replay/sample_cases.jsonl --json

# M1 end-to-end smoke (upload → parse → publish → query → feedback)
python scripts/m1_smoke.py --json
python scripts/m1_smoke.py --cluster http://127.0.0.1:8070    # R35-C: drive a live cluster via api-gateway instead of in-process TestClient

# Real Kubernetes deployment smoke (R34–R35): creates a kind cluster,
# loads 8 service images + postgres, installs the helm chart with the
# smoke overlay, waits for pods Ready, runs curl checks + a real
# agent-run round-trip + lists PG tables.  Idempotent / re-runnable.
bash scripts/kind-smoke.sh

# Postgres migrations
MIGRATION_DATABASE_URL=$DATABASE_URL alembic upgrade head
```

`pytest.ini` already declares the pythonpath for every `services/*/src` and `packages/*/src` directory — there is no need to set `PYTHONPATH` manually. New services must add themselves to `pytest.ini`'s `pythonpath` and `pyproject.toml`'s `[tool.setuptools]` block (see existing entries for the pattern). `test_local_ci.py` is excluded from normal runs because it recursively invokes `pytest` and the Windows sandbox can't run frontend/lint subprocesses; CI runs it on Linux.

## Conventions

### Backwards compatibility

Every round (R17–R35) preserves existing API contracts. New fields on Pydantic models are `Optional` with defaults; new endpoint behaviour is gated by env vars; old endpoints keep their signatures. When refactoring a shared type (e.g., `IncidentResponse`), check that all sample fixtures still load.

### TDD red→green→commit

The repo follows strict TDD. Each commit is a single TDD cycle:

1. Write a failing test that pins the desired behaviour (often a regression test).
2. Run pytest, see it fail.
3. Implement the minimum to make it pass.
4. Run the full suite to confirm no regression.
5. `ruff check --fix` + `ruff format`, then commit with a `feat(rNN-M.N): <verb> <noun>` prefix.

Commits are fine-grained: one endpoint or one module per commit. The default branch is `master`; pushes go directly there.

### Pluggable clients

When a service needs to depend on another, define a `Protocol` in a shared module, then implement `InMemory*Client` (for tests, binds to in-process state) and `Http*Client` (for production). A `build_*_client()` factory picks the right one from env. Tests monkeypatch `httpx.get`/`httpx.post` to drive the HTTP path without opening sockets — see `tests/test_agent_platform_mcp_delegation.py` and `tests/test_agent_platform_approval_delegation.py` for the canonical pattern.

### Six-factor hypothesis ranking (RCA only)

`generate_hypotheses(incident, evidence, *, topology_deps=None)` always emits three candidates (`transmission_link_degradation`, `wireless_access_anomaly`, `recent_parameter_change`) sorted by `_score_hypothesis`. The score combines base prior (with alarm-code match), time_relevance (0.20), topology_distance (0.15), kpi_strength (0.20), history_similarity (0.10), sop_match (0.15), minus counter_evidence (0.20). When no evidence has `contradicts_root_cause=True`, the pre-R23 swap-back behaviour populates `contradicting_evidence_ids` from the other hypothesis's supporting list — this is what `tests/test_rca_replay_m3.py` relies on for top-1 coverage.

### State machine guards

The approval state machine (8 states: `pending`, `supplement_pending`, `transferred`, `escalated`, `approved`, `rejected`, `expired`, `pending_supplement`) lives in `services/agent-platform-api/src/ai_employee/agent_platform_api/runtime.py`. New governance endpoints go through `runtime._get_task()` (raises `ApprovalTaskNotFound`) and `runtime._require_open(task)` (raises `ApprovalTaskNotModifiable`). Both map to 404 / 409 in `app.py` via try/except — never bypass.

### LangGraph runtime (agent-platform-api)

`services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py` is the v1 `StateGraph` runtime selected by `RUNTIME_BACKEND=langgraph` (default `dag`). It uses `Send` API for parallel fan-out (ToolPlan → ToolExec workers → ToolAggregate reducer; R32-B) and an `interrupt_before` checkpoint for HITL approval, resumed via `graph.update_state` + `graph.invoke(None, config)`.

All R33-A depth features are **env-gated and off by default** (existing templates unaffected):
- `CHECKPOINTER_BACKEND` ∈ {`memory` (default), `redis`, `postgres`} — `build_checkpointer()` picks the saver; redis/postgres deps degrade to MemorySaver when absent. `REDIS_CHECKPOINT_URL` / `POSTGRES_CHECKPOINT_URL` supply the DSN.
- `LANGGRAPH_INTERRUPT_NODES` (default `ApprovalRequired`, comma-separated) — which nodes pause for HITL. R33-A2 adds a `SupplementRequired` node + `resume_from_supplement()`, gated by `LANGGRAPH_SUPPLEMENT_GATE=true`.
- `LANGGRAPH_PARALLEL_RETRIEVAL=true` — fans out one `KnowledgeRetrieve` worker per declared knowledge scope (R33-A3).

### Health endpoint honesty (R35-A)

`/health` on knowledge-api, agent-platform-api, and rca-agent introspects the **actual** store class / runtime selector (not a hardcoded string). When wiring a new backend, update the health dict to read from the constructed store/runtime so it stays honest — the R34 smoke caught a hardcoded `"storage": "sqlite"` that lied when PG was wired.

### Observability

Seven headline indicators (`agent_run_success_rate`, `approval_wait_time_p95_s`, `report_acceptance_rate`, `model_latency_p95_ms`, `tool_latency_p95_ms`, `fallback_rate`, `tool_call_success_rate`) live in `packages/common-schemas/src/ai_employee/common_schemas/metrics_bridge.py` as a process-wide singleton. The agent-platform re-exports its richer `PlatformMetrics` via `PLATFORM_METRICS_FROM_AGENT_PLATFORM=1`. Wiring `record_*` calls into the bridge from new hot paths is preferable to writing new metric stores.

### Rate limiting

`packages/rate-limit/` provides `install_rate_limiter(app)` (env `RATE_LIMIT_ENABLED=false` by default → no-op) and is mounted into all 6 user-facing services. To add a new endpoint dimension (per-tenant, per-tool), pass `key_func` rather than copying the middleware.

### Object store

`packages/object-store/` exposes `ObjectStore` Protocol with `LocalFsObjectStore` (default, no deps) and `S3ObjectStore` / `MinioObjectStore` (boto3). `build_object_store()` picks from `OBJECT_STORE_URL`. Approval-supplement attachments and knowledge raw files both go through this; do not write to `var/objects/` directly.

### Helm chart & deployment overlays

`infra/helm/` is a single chart rendering all 9 HTTP services. Three values files:
- `values.yaml` — dev baseline (replicas 1–2, no resources, auth/ratelimit off, `databaseUrl` points at in-cluster `postgres:5432`).
- `values-smoke.yaml` — kind smoke overlay (SQLite or in-cluster PG, auth open, `event-gateway` enabled pointing at `kafka:9092`). Applied with `helm install ai-emp infra/helm -f values.yaml -f values-smoke.yaml`.
- `values-prod.yaml` (R33-F) — production overlay: flips `API_GATEWAY_AUTH_REQUIRED=true`, `RATE_LIMIT_ENABLED=true`, `jwtAuthStrict=true`, replicas 2+, per-service resources, HPA, ingress+TLS, OIDC placeholders. **Code defaults stay false** — production enforcement is overlay-only.

**Chart gotchas (all fixed in R34, documented so they don't regress):**
- The chart does NOT template the namespace — the operator creates it (`kubectl create namespace ai-employee`); `helm install --create-namespace` handles labeling. (R35-B removed `templates/namespace.yaml` because helm-managing the namespace made `helm uninstall` delete the standalone postgres too.)
- `readinessProbe` is `/health/ready` only on `agent-platform-api`; the other 8 services expose only `/health` — the deployment template picks per service.
- `runAsUser`/`fsGroup` must be `10001` to match the Dockerfile `appuser` uid (was 1000 → permission denied writing `./var`).
- Secret refs point at the single `ai-employee-secrets` Secret with UPPER_SNAKE keys (`INTERNAL_TOKEN`, not camelCase-derived `INTERNALTOKEN`).
- `hasStorage` helper (`_helpers.tpl`) uses `regexFind "^[0-9]+"` + `atoi` — Helm's `int "1Gi"` coerces to 0 and suppressed every PVC. Use the helper for any storage-conditional.
- `DATABASE_URL` is injected only `{{- with $global.databaseUrl }}` (empty → omitted → SQLite fallback); never `default` it to a hard-coded PG URL.

**In-cluster Postgres (R34-D3):** `infra/k8s/postgres.yaml` is a minimal single-writer Postgres 16 Deployment + Service `postgres:5432` + 2Gi PVC, applied separately from the chart (not helm-managed). **Not for production** — point `global.databaseUrl` at managed PG instead. **Redpanda (R35-D):** `infra/k8s/redpanda.yaml` is the analogous KRaft-mode Kafka-compatible broker (Service `kafka:9092`) for the event-gateway smoke.

**Known kind/Windows blocker:** `kind load docker-image` of multi-platform manifests (prometheus, grafana, redpanda, postgres:*-alpine) fails with `ctr: content digest ... not found`. `postgres:16` (non-alpine) loads cleanly; the kind-smoke script falls back to checking the image is already in the node on re-runs. Production deploys on Linux are unaffected.

### Redis pub/sub for multi-replica

`RedisEventBus` (in `services/agent-platform-api/src/ai_employee/agent_platform_api/events.py`) bridges events across replicas when `EVENT_BUS_BACKEND=redis`. Per-replica dedup keeps the loop from re-publishing what it already received from the channel.

### Commit / push gotcha

Avoid running plain `git add -A` from the repository root: it sweeps in the `.claude/worktrees/*` directories and staged-deletes files that exist in HEAD but are missing from the working tree (e.g., R19's `tests/test_echarts_endpoint.py`). Always stage explicit paths. To restore a damaged working tree: `git reset HEAD && git checkout -- . && git status`.

## Repo-specific pointers

- **DocSuite is huge**: 1815+ tests across `tests/` plus per-package `packages/<name>/tests/`. New work typically adds 5–30 tests per round.
- **Worktrees are heavy**: workflows spawn isolated `git worktree`s under `.claude/worktrees/wf_*/`. Don't `git clean` — there will be dozens of stale worktrees. Cleanup is done by the workflow runner when review completes.
- **`Docs/superpowers/specs/`** holds per-round design notes (`2026-06-19-r{19..27}-<topic>.md` and `2026-06-23-r{33,34}-*.md`). Read the most recent one before starting work in its area — it records what was deliberately left as "known leftover" so you don't redo it.
- **Web portal `apps/web-portal/`** has its own Vitest suite; the tests run on Linux CI but fail in the Windows sandbox due to missing `pnpm` and shell quirks (see `tests/test_local_ci.py::test_frontend_vitest_passes` — pre-existing, unrelated).
- **`mypy.ini`** is configured but the suite is not run in CI. Don't worry about typing warnings, but do keep `from __future__ import annotations` at the top of every new module (the runtime imports use forward references).
- **Skill install reminder**: there is no CLAUDE.md before this file; if `.claude/skills/` references a skill not installed locally, follow the path resolution in the project-level `~/.claude/settings.json` rather than re-implementing.