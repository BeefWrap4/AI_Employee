# R33 — Progressive items closure (2026-06-23)

Closes the 15 documented post-MVP progressive items left open after R32's
spec alignment.  All work is strict TDD red→green→commit, env-gated /
additive by default, backward compatible.  Delivered via two ultracode
multi-agent workflow rounds (6 + 2 worktree-isolated agents on disjoint
file sets, merged sequentially to master).

Baseline → final: **1663 → 1763 passed**, 16 skipped (all legitimate
env-gated, catalogued in `2026-06-23-r33-skip-tests-audit.md`), 0 failed.
`ruff check .` clean.

## Item → commit map

| # | Progressive item | Round | Sub-id | Commits |
|---|---|---|---|---|
| 1 | LangGraph multi-gate interrupt | R1 | r33-a2 | `365cb49` |
| 2 | Cross-replica checkpointer | R1 | r33-a1 | `326dc1f` |
| 3 | Parallel subgraph extension | R1 | r33-a3 | `61c9d74` |
| 4 | rca template real third-party | R1 | r33-b | `f7ca940` |
| 5 | OCR image handler | R1 | r33-c1 | `997fb67` |
| 6 | Table structured fields + DOCX tables | R1 | r33-c2/c3 | `f7b3541`, `7a2620c` |
| 7 | SSE streaming output | R1 | r33-d | `4d1d61a` |
| 8 | Skip-test audit | R2 | r33-e | `ed710de` |
| 9 | test_local_ci Windows | R2 | r33-e | (catalogued; no code change needed) |
| 10 | Auth default flip (prod) | R1 | r33-f1 | `efe7c54` |
| 11 | Rate-limit default flip (prod) | R1 | r33-f1 | `efe7c54` |
| 12 | Checkpointer factory | R1 | r33-a1 | `326dc1f` |
| 13 | Distributed trace propagation | R2 | r33-h1/h2 | `31dbe1e`, `96a3ac0` |
| 14 | Grafana dashboard + prometheus.yml | R1 | r33-g1a/b/c | `52a4104`, `b8d5c48`, `6a7dc12` |
| 15 | Helm prod values overlay | R1 | r33-f1/f2 | `efe7c54`, `2f0bab3` |

## What each delivers

**R33-A (langgraph depth)** — `langgraph_runtime.py`:
- `build_checkpointer()` env factory: `CHECKPOINTER_BACKEND` ∈
  {memory (default), redis, postgres}; optional deps degrade to
  MemorySaver.  `_get_checkpointer()` wired to it.
- Multi-gate: `SupplementRequired` interrupt node +
  `LANGGRAPH_INTERRUPT_NODES` config + `resume_from_supplement()`.
  Gated by `LANGGRAPH_SUPPLEMENT_GATE` (default off).
- Parallel multi-source retrieval: `KnowledgeRetrieve`/`KnowledgeAggregate`
  nodes via `Send` fan-out, one worker per declared knowledge scope.
  Gated by `LANGGRAPH_PARALLEL_RETRIEVAL` (default off).

**R33-B** — `tests/test_r33b_rca_template_real_thirdparty.py`: the rca
template's two tools (`rca-agent.runs.create`, `rca-agent.reports.review`)
exercised through a real mcp-gateway (TestClient) whose handlers call the
real rca-agent HTTP endpoints over httpx.  Completes 5/5 template
three-party coverage.

**R33-C (ingestion depth)** — `parsers.py`/`chunker.py`/`knowledge.py`:
- `ImageParser` registered for `image/png`+`image/jpeg`, delegates to
  `build_ocr_backend()`; degrades to a placeholder section when OCR is
  disabled (never 500s).
- `ParsedChunk.columns`/`values` Optional fields (backward compat);
  `XlsxParser` populates them; chunker propagates onto row chunks.
- `DocxParser` now iterates `doc.tables` → one `ParsedSection` per table
  with columns/values.

**R33-D** — `app.py`: `GET /api/v1/agent-runs/{run_id}/stream` SSE
endpoint (text/event-stream) replaying bus history then streaming live
`RunEvent`s; clean unsubscribe on disconnect.  WebSocket endpoint
untouched.

**R33-E** — `Docs/superpowers/specs/2026-06-23-r33-skip-tests-audit.md` +
`tests/test_r33_skip_audit.py`: every `pytest.skip`/`skipif`/`importorskip`
catalogued (Postgres-live, Windows-WAL, CLI-tool, OCR-backend,
checkpointer-extras, optional-deps).  Verdict: zero broken/masked skips,
zero `xfail`.  Pinning test guards the doc against drift.

**R33-F** — `infra/helm/values-prod.yaml` + `infra/helm/README.md`:
production overlay (`helm install ... -f values.yaml -f values-prod.yaml`).
Flips `API_GATEWAY_AUTH_REQUIRED=true`, `RATE_LIMIT_ENABLED=true`,
`jwtAuthStrict=true`, replicas (knowledge-api 1→2), per-service
resources requests/limits, HPA on rca-agent/api-gateway/mcp-gateway,
ingress+TLS, OIDC placeholders.  **Code defaults stay false** — prod
enforcement is overlay-only.

**R33-G1** — `infra/observability/`: `prometheus.yml` (scrape all 9
service ports + self), Grafana datasource provisioning, 7-panel
`agent-platform.json` dashboard (one stat per headline indicator, metric
names `platform_*` from `metrics_bridge.to_prometheus_text`), dashboard
provider config, docker-compose grafana volume mount.

**R33-H** — `clients.py`+`app.py`: `bind_trace_context()` contextvar +
`_trace_headers()`; `HttpApprovalServiceClient`/`HttpMcpGatewayClient`
`_headers()` propagate `X-Trace-Id`/`X-Run-Id` only when a context is
active (backward compat).  Platform `trace_context_middleware` binds the
context from the inbound request header (mints when absent), closing the
api-gateway → platform → approval-service/mcp-gateway trace chain.

## Verification

- `pytest tests/ --ignore=tests/test_local_ci.py` → 1763 passed, 16
  skipped, 0 failed.
- `ruff check .` → All checks passed.
- All new behavior env-gated / off by default; no existing contract
  changed.

## Known leftover (non-blocking)

- `langgraph-checkpoint-redis` / `-postgres` packages are not installed
  in this env, so the redis/postgres checkpointer paths skip cleanly
  (factory + degradation verified; `RedisSaver`/`PostgresSaver` activate
  once the dep is installed in a deployment env).
- Push of the Round-2 commits to origin was blocked by a transient
  GitHub connectivity outage at close of session; local master holds the
  complete, verified state.
