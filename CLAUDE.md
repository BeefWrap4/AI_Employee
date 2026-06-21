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

### Services

`services/` houses the eight FastAPI apps; `packages/` holds libs that multiple services import. Service ports follow the convention `8010–8060` (10-step), allocated in the order they were extracted.

```
services/knowledge-api          (8010)  RAG knowledge base (project-1)
services/ingestion-worker       (8011)  PDF/DOCX/XLSX/MD parsing + embedding
services/rca-agent              (8020)  Alarm RCA pipeline (project-2)
services/agent-platform-api     (8030)  Agent runtime, approvals, tools (project-3)
services/tool-registry          (8040)  MCP tool registry
services/approval-service       (8040)  Standalone approval task service (post-R21)
services/mcp-gateway            (8050)  MCP protocol gateway (post-R21)
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

## Common commands

```bash
# Setup (Miniconda is the canonical env)
conda env create -f environment.yml
conda activate ai-employee
pip install -e ".[dev]"

# Run full test suite (all packages)
pytest                                  # ~1500 tests, ~2 min

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

# Postgres migrations
MIGRATION_DATABASE_URL=$DATABASE_URL alembic upgrade head
```

`pytest.ini` already declares the pythonpath for every `services/*/src` and `packages/*/src` directory — there is no need to set `PYTHONPATH` manually. New services must add themselves to `pytest.ini`'s `pythonpath` and `pyproject.toml`'s `[tool.setuptools]` block (see existing entries for the pattern). `test_local_ci.py` is excluded from normal runs because it recursively invokes `pytest` and the Windows sandbox can't run frontend/lint subprocesses; CI runs it on Linux.

## Conventions

### Backwards compatibility

Every round (R17–R27) preserves existing API contracts. New fields on Pydantic models are `Optional` with defaults; new endpoint behaviour is gated by env vars; old endpoints keep their signatures. When refactoring a shared type (e.g., `IncidentResponse`), check that all sample fixtures still load.

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

### Observability

Seven headline indicators (`agent_run_success_rate`, `approval_wait_time_p95_s`, `report_acceptance_rate`, `model_latency_p95_ms`, `tool_latency_p95_ms`, `fallback_rate`, `tool_call_success_rate`) live in `packages/common-schemas/src/ai_employee/common_schemas/metrics_bridge.py` as a process-wide singleton. The agent-platform re-exports its richer `PlatformMetrics` via `PLATFORM_METRICS_FROM_AGENT_PLATFORM=1`. Wiring `record_*` calls into the bridge from new hot paths is preferable to writing new metric stores.

### Rate limiting

`packages/rate-limit/` provides `install_rate_limiter(app)` (env `RATE_LIMIT_ENABLED=false` by default → no-op) and is mounted into all 6 user-facing services. To add a new endpoint dimension (per-tenant, per-tool), pass `key_func` rather than copying the middleware.

### Object store

`packages/object-store/` exposes `ObjectStore` Protocol with `LocalFsObjectStore` (default, no deps) and `S3ObjectStore` / `MinioObjectStore` (boto3). `build_object_store()` picks from `OBJECT_STORE_URL`. Approval-supplement attachments and knowledge raw files both go through this; do not write to `var/objects/` directly.

### Redis pub/sub for multi-replica

`RedisEventBus` (in `services/agent-platform-api/src/ai_employee/agent_platform_api/events.py`) bridges events across replicas when `EVENT_BUS_BACKEND=redis`. Per-replica dedup keeps the loop from re-publishing what it already received from the channel.

### Commit / push gotcha

Avoid running plain `git add -A` from the repository root: it sweeps in the `.claude/worktrees/*` directories and staged-deletes files that exist in HEAD but are missing from the working tree (e.g., R19's `tests/test_echarts_endpoint.py`). Always stage explicit paths. To restore a damaged working tree: `git reset HEAD && git checkout -- . && git status`.

## Repo-specific pointers

- **DocSuite is huge**: 1500+ tests across `tests/` plus per-package `packages/<name>/tests/`. New work typically adds 5–30 tests per round.
- **Worktrees are heavy**: workflows spawn isolated `git worktree`s under `.claude/worktrees/wf_*/`. Don't `git clean` — there will be dozens of stale worktrees. Cleanup is done by the workflow runner when review completes.
- **`Docs/superpowers/specs/`** holds per-round design notes (`2026-06-19-r{19..27}-<topic>.md`). Read the most recent one before starting work in its area — it records what was deliberately left as "known leftover" so you don't redo it.
- **Web portal `apps/web-portal/`** has its own Vitest suite; the tests run on Linux CI but fail in the Windows sandbox due to missing `pnpm` and shell quirks (see `tests/test_local_ci.py::test_frontend_vitest_passes` — pre-existing, unrelated).
- **`mypy.ini`** is configured but the suite is not run in CI. Don't worry about typing warnings, but do keep `from __future__ import annotations` at the top of every new module (the runtime imports use forward references).
- **Skill install reminder**: there is no CLAUDE.md before this file; if `.claude/skills/` references a skill not installed locally, follow the path resolution in the project-level `~/.claude/settings.json` rather than re-implementing.