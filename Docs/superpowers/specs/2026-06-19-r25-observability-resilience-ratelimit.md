# R25 — Observability / Resilience / Rate-limit (2026-06-19)

目标：补齐真实中间件与治理深度项中的可观测、工具韧性、限流网关化三块。

## R25-T — 工具韧性 (已推送 e2adae5 + e6cb46f)

`services/tool-registry/app.py:invoke_tool` 接 `tool_resilience.apply_resilience`：
- `ToolSpec.timeout_ms` (默认 5000) 强制超时 → 408
- `ToolSpec.retry_policy.max_attempts/backoff_seconds` 重试 → 耗尽 504
- `PlatformCircuitBreaker` open → 503

`services/tool-registry/health_probe.py`（新增）：`probe_and_persist` 后台探针按 `health_check_url` 写回 `health_status` (`unknown`→`healthy`/`unhealthy`)。

`services/rca-agent/http_resilience.py`（新增）：`resilient_fetch(op, timeout_ms=)` 墙钟超时 + env-driven retry（`RL_HTTP_RETRY_MAX_ATTEMPTS` 默认 1）。所有四个 RCA HTTP adapter（Prometheus KPI / Elasticsearch log / Neo4j topology / Ticket API）接同一层，零调用点改动。

新增 19 测试；回归 49 个 tool-registry + rca-adapter 测试全绿。

## R25-O — 可观测埋点 (本提交)

`packages/common-schemas/src/ai_employee/common_schemas/metrics_bridge.py`（新增）：跨包 `PlatformMetrics` 单例 + `platform_metrics()` accessor + `snapshot_dict()` + `to_prometheus_text()`。当 `PLATFORM_METRICS_FROM_AGENT_PLATFORM=1` 时自动 monkey-patch 指向 agent-platform 的真实 metrics 单例。

`packages/llm-gateway/src/ai_employee/llm_gateway/client.py`：LlmClient 新增可选 `on_success(latency_ms)` 回调，在 chat 成功后调用。knowledge-api 通过 `on_success=lambda ms: platform_metrics().record_model_latency(ms)` 接线。

`services/knowledge-api/src/ai_employee/knowledge_api/app.py:_answer_query`：LLM 成功路径调 `record_model_latency`。

`metrics_bridge.snapshot_dict()` 输出 7 指标（含 `model_latency_p95_ms` / `tool_latency_p95_ms` / `fallback_rate`）。`to_prometheus_text()` 渲染 Prometheus text 格式七指标 series。

新增 10 测试覆盖七指标 record/snapshot/Prometheus 渲染。

## R25-L — 限流网关化 (本提交)

`services/agent-platform-api/src/ai_employee/agent_platform_api/rate_limit*.py` 抽出到共享包 `packages/rate-limit/src/ai_employee/rate_limit/`，保留 agent-platform 内的 re-export 兼容（`rate_limit_redis.py` + `rate_limit_middleware.py`）。

新包结构：
- `limiter.py` — `SlidingWindowLimiter` + `InMemoryBackend` / `RedisBackend` + `build_sliding_window_limiter()` (env-driven)
- `middleware.py` — `RateLimitMiddleware` + `install_rate_limiter(app)` 一行接入

`env`：`RATE_LIMIT_ENABLED` (默认 false → no-op), `RATE_LIMIT_LIMIT` (60), `RATE_LIMIT_WINDOW_SECONDS` (60), `REDIS_URL` (可选)。

接入 6 服务（agent-platform 早已接入；本轮加：knowledge-api / rca-agent / tool-registry / mcp-gateway / approval-service / ingestion-worker）。每个 `create_app` 加 `install_rate_limiter(app)`。

新增 9 包测试 + 7 迁移测试 = 16 新测试；12 个 agent-platform 原 rate_limit 测试通过 re-export 全绿。

## 测试统计

| 切片 | 通过 | 跳过 |
|---|---|---|
| R25-T | 19 + 49 回归 = 68 | 0 |
| R25-O | 10 | 0 |
| R25-L | 9 + 12 回归 = 21 | 0 |
| **总新增** | **50** | **0** |
| **全套 (master + R25)** | **1511** | **12** |

## 关键文件

- `packages/rate-limit/src/ai_employee/rate_limit/{__init__,limiter,middleware}.py`
- `packages/rate-limit/tests/test_rate_limit_pkg.py`
- `packages/common-schemas/src/ai_employee/common_schemas/metrics_bridge.py`
- `packages/llm-gateway/src/ai_employee/llm_gateway/client.py` (on_success hook)
- `services/tool-registry/src/ai_employee/tool_registry/{app,store,health_probe}.py`
- `services/rca-agent/src/ai_employee/rca_agent/{http_resilience,tool_adapters}.py`
- 6 服务 `create_app` 加 `install_rate_limiter(app)`
- `pyproject.toml` + `pytest.ini` 注册 `ai_employee.rate_limit` 包

## 已知遗留

- `tool_call_success_rate` 仍走 `_tool_call_success_rate()`（从 `PlatformToolCallLogStore` 读）；该 store 仍是 R22 之前的死表（无生产 writer）。R25-O + R25-L 本轮未触及 — 留 R26+ 推跨服务 `tool-call-logs` 聚合上报。
- 健康检查探针仍按需触发（on-demand GET `/tools/{name}/health`），未挂后台定时 sweep。
- R25-T 默认 `timeout_ms=5000` / `max_attempts=1` / `backoff_seconds=0.0` 等同 pre-R25 行为；生产部署需显式提升。
- 限流仅按 X-User-Id → IP 单 key，未做 per-tenant / per-endpoint 维度（`install_rate_limiter` 后续可加 `key_func` 参数）。