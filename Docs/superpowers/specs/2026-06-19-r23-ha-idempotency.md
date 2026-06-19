# Spec — R23 收尾：高可用（多副本）+ 幂等性

日期：2026-06-19
主题：R23 阶段交付总览——`IdempotencyStore`（InMemory / Redis 双后端）+ `Idempotency-Key` 接线 + `RedisEventBus` 多副本事件总线 + Helm 多副本 values + HA 文档 + leader-failover 回归测试
适用范围：`packages/common-schemas`、`services/agent-platform-api`、`services/knowledge-api`、`infra/helm`、`tests/`

## 1. 目标 (Goal)

R22 之前所有服务 `replicas=1`（SQLite 单写、本地卷、单进程 `EventBus`），跨项目差距分析（`Docs/gap-analysis-2026-06-18.md` §三/§四）把「§9 高可用（多副本/幂等/备份）」标为 🔴、把「限流 / 事件网关」标为 P1。R20–R22 已经把审批状态机、MCP/审批独立服务、对象存储补齐，但只要 `agent-platform-api` 抬到 2 副本就会同时引入三个正确性裂缝：

1. **调度器重复触发**：每个副本都各自 `tick` cron 调度，同一 schedule 被 N 个副本各 fire 一次。
2. **副作用重复执行**：客户端 timeout 后重试 `POST /api/v1/agent-runs`，或负载均衡把重试打到另一副本，会创建第二条 run。
3. **WebSocket 事件丢失**：`/api/v1/ws/runs/{run_id}` 订阅的是进程内 `EventBus` 单例，副本 A 上发布的事件，副本 B 上的订阅者永远收不到。

R23 把这三条裂缝一次性补上，让「抬副本」从「会出事」变成「按文档配齐 Postgres+Redis+对象存储后即可抬」。具体目标：

1. 新增 `packages/common-schemas/idempotency.py`：`IdempotencyStore` Protocol + `InMemoryIdempotencyStore`（单进程，测试/dev）+ `RedisIdempotencyStore`（Redis hash + TTL，多副本共享键空间，Redis 宕机 fail-open 重执行）+ `build_idempotency_store()` 工厂（`REDIS_URL` 设置走 Redis，不可达优雅降级 InMemory）。
2. 把 `Idempotency-Key` 头接进 `POST /api/v1/agent-runs`、`POST /api/v1/evaluations/runs`、`POST /api/v1/documents` 三个可重试副作用端点：重试命中缓存即原样回吐（同一 `run_id` / `eval_run_id` / `doc_id`），不重复执行。文档上传 key = `Idempotency-Key + sha256(content)`，同 key 不同字节仍建新文档。
3. 新增 `RedisEventBus`：包装进程内 `EventBus`，`publish` 先本地落历史 + 本地扇出，再 mirror 到 Redis pub/sub 频道；每副本跑一个后台 listener 把收到的消息扇回本地总线，副本 B 上的 WebSocket 订阅者因此能收到副本 A 发布的事件。listener 路径不 re-publish、不重复记录历史（防环）。Redis 不可达时降级为本地-only。`build_multi_replica_event_bus()` 在 `REDIS_URL` 可达时选 Redis、否则选进程内单例，通过 `EVENT_BUS_BACKEND=redis` 在 app lifespan 接入。
4. Helm `values.yaml` 把 `ingestion-worker` / `rca-agent` / `tool-registry` / `approval-service` / `mcp-gateway` 副本数抬到 2（`agent-platform-api` 已为 2），新增 `approval-service` + `mcp-gateway` 为 chart 一等公民条目，`agent-platform-api` 注入 `EVENT_BUS_BACKEND: redis`；`knowledge-api` 仍保持 1（SQLite 单写，待 `pg_store` 默认后再抬）。新增 `infra/helm/HA.md` 文档 HA 前置条件、各子系统 failover 故事、健康/关停探针。
5. 新增 `tests/test_ha_leader_failover.py` 模拟双副本 + 共享 fake-Redis lease：只有 leader tick、standby `run_once` 空转；leader 释放 lease（崩溃）后 standby 下一次 tick 接管，无 schedule 双 fire、无 tick 丢失；lease TTL 过期后老 leader 停 tick、standby 接管；leader 跨 tick 走 renew 不重新 acquire。同步更新 `test_helm_templates.py` 期望 7 个服务（R23 新增 `approval-service` + `mcp-gateway`）各渲染出 Deployment。

> 注：R22 spec §5「下一步建议 (R23 Candidates)」原列对象存储深化项（Range 下载、写穿强一致、key ACL、迁移脚本、MinIO 分布式、生命周期）。R23 实际选了 HA/幂等主线（差距分析 P1-7 限流配套 + §三 🔴 高可用），因为「抬副本」是 P0 阻塞演示的能力，且 leader 选举/事件总线/幂等缓存都以 Redis 为底座，一次性接入性价比最高。R22 §5 列表降级为 R24+ 候选。

## 2. 实现清单 (Deliverables)

### 2.1 `packages/common-schemas/idempotency.py`（r23-1，新模块）

| 符号 | 变更要点 |
| --- | --- |
| `IdempotencyRecord` | 数据类：`key` / `status`（`in_flight` \| `success` \| `failed`）/ `result: dict \| None` / `created_at`；`to_dict()` 序列化。 |
| `IdempotencyStore` (Protocol) | `@runtime_checkable` 协议：`get_or_begin(key) -> IdempotencyRecord`（未知/TTL 过期 → 标 `in_flight` 并返回，调用方获得执行权；已 `in_flight` → 返回 marker 供调用方短路；`success`/`failed` → 返回缓存记录供重放原始响应）；`complete(key, *, status, result)`（记录终态，未 begin 的 key 防御性 no-op）。 |
| `InMemoryIdempotencyStore` | dict + `threading.Lock`，`ttl_s=86400`，单进程；测试与单副本 dev 用。多副本必须走 Redis 后端共享键空间。 |
| `RedisIdempotencyStore` | 每 key 一个 Redis hash + TTL，缓存结果对所有副本可见并自动过期；Redis 宕机时 `get_or_begin`/`complete` fail-open（视为未缓存 → 重执行），保证可用性优先于严格幂等。 |
| `build_idempotency_store()` | 按 env 选后端：`REDIS_URL` 设置 → Redis（不可达优雅降级 InMemory）；否则 InMemory。 |
| `packages/common-schemas/src/ai_employee/common_schemas/__init__.py` | 导出 `IdempotencyStore` / `IdempotencyRecord` / `InMemoryIdempotencyStore` / `RedisIdempotencyStore` / `build_idempotency_store`。 |

### 2.2 服务接线（r23-2）

| 模块 | 文件 | 变更要点 |
| --- | --- | --- |
| `services/agent-platform-api` | `src/ai_employee/agent_platform_api/app.py` | `create_app(idempotency_store=...)` 注入，默认 `build_idempotency_store()`。`POST /api/v1/agent-runs`、`POST /api/v1/evaluations/runs` 读 `Idempotency-Key` 头：首次请求 `get_or_begin` 标 `in_flight` → 执行 → `complete(success/failed, result=<响应体>)`；重试命中 `success` 记录直接回吐原始响应（同一 `run_id` / `eval_run_id`），命中 `in_flight` 短路返回 409。审批决策端点已由 R20-5 终态守卫覆盖，不重复接入。 |
| `services/knowledge-api` | `src/ai_employee/knowledge_api/app.py` | `POST /api/v1/documents` 同样读 `Idempotency-Key`，但 key = `Idempotency-Key + sha256(content)`：同 key + 同字节 → 命中缓存回吐原 `doc_id`；同 key + 不同字节 → 视为新文档（防止「同 key 改内容」被静默吞掉）。 |

### 2.3 RedisEventBus 多副本事件总线（r23-3）

| 符号 | 变更要点 |
| --- | --- |
| `RedisEventBus` (`events.py`) | 包装进程内 `EventBus` 单例。`publish` 顺序：先 `_local.publish`（本地落历史 + 本地扇出，保证发布者自己的订阅者立即看到）→ 再 `redis.publish(channel, json)`。后台 listener 线程 `start_listener()` 订阅该频道，收到消息 `_local._fan_out` 扇回本地总线（副本 B 的 WebSocket 订阅者因此收到副本 A 的事件）。listener 路径**不**再 publish 回 Redis、**不**重复记录历史（防环 + 防重）。Redis 不可达时 `publish` 降级为本地-only（warning 日志，不抛）。 |
| `build_multi_replica_event_bus()` | `REDIS_URL` 可达 → `RedisEventBus`；否则进程内 `EventBus` 单例。 |
| app lifespan | `EVENT_BUS_BACKEND=redis` 时在 FastAPI lifespan 启动 listener 线程、关停时释放（与 scheduler lease、Redis 连接一起在 lifespan 生命周期内管理）。 |

### 2.4 Helm 多副本 values + HA 文档（r23-4）

| 文件 | 变更要点 |
| --- | --- |
| `infra/helm/values.yaml` | `ingestion-worker` / `rca-agent` / `tool-registry` / `approval-service` / `mcp-gateway` 副本数 → 2（`agent-platform-api` 已 2）；新增 `approval-service` + `mcp-gateway` 为 chart 一等公民条目；`agent-platform-api` 注入 `EVENT_BUS_BACKEND: redis`；`knowledge-api` 保持 1（SQLite 单写，注释标注「待 `pg_store` 默认后再抬」）。 |
| `infra/helm/HA.md` (新) | TL;DR 表（每服务默认副本 / HA 安全副本 / 所需状态后端）；HA 前置条件（Postgres + Redis + 对象存储）；各子系统 failover 故事（leader lease、idempotency cache、Redis event bus、shared rate limit）；健康/关停探针（`/health` liveness、`/health/ready` readiness、SIGTERM graceful shutdown）。 |

### 2.5 测试

| 测试文件 | 用例 | 覆盖点 |
| --- | --- | --- |
| `tests/test_idempotency.py` (新) | 多断言 | `IdempotencyRecord` 序列化往返；`InMemoryIdempotencyStore` `get_or_begin`/`complete` 全生命周期（未知→`in_flight`→`success`/`failed`）；TTL 过期后重新 begin；`RedisIdempotencyStore` 经 fakeredis 跑同样路径；Redis 宕机 fail-open 重执行；`build_idempotency_store` env 选择 + 降级；Protocol `isinstance` 静态满足。 |
| `tests/test_idempotency_endpoints.py` (新) | 多断言 | `POST /api/v1/agent-runs` 同 `Idempotency-Key` 重试回吐原 `run_id` 不重创；`POST /api/v1/evaluations/runs` 同；`POST /api/v1/documents` 同 key+同字节回吐原 `doc_id`、同 key+不同字节建新文档；`in_flight` 并发短路 409；无 key 头走原路径。 |
| `tests/test_redis_event_bus.py` (新) | 多断言 | `RedisEventBus.publish` 本地落历史 + 本地扇出 + Redis mirror（fakeredis）；listener 把副本 A 事件扇回副本 B 本地总线；listener 不 re-publish、不重复历史（防环）；Redis 不可达降级本地-only；`build_multi_replica_event_bus` env 选择。 |
| `tests/test_ha_leader_failover.py` (新) | 4 场景 | 双副本 + 共享 fake-Redis lease：仅 leader tick（standby `run_once` 空转）；leader 释放 lease（崩溃）后 standby 下一次 tick 接管，无 schedule 双 fire、无 tick 丢失；lease TTL 过期后老 leader 停 tick、standby 接管；leader 跨 tick 走 renew 不重新 acquire。 |
| `tests/test_helm_templates.py` (更新) | +断言 | 期望 7 个服务（R23 新增 `approval-service` + `mcp-gateway`）各渲染出 Deployment。 |

合计 +1856 行（12 文件），其中 5 个 R23 测试文件全部走 `TestClient` + `fakeredis` + 进程内 lease 模拟，无需真实 Redis / Postgres。

## 3. 测试结果 (Test Results)

- R23-5 提交信息记录全量回归：**1416 passed, 12 skipped, 0 failed**（in-memory 模式，无真实中间件）。
- 新增 5 个 R23 测试文件（`test_idempotency.py` / `test_idempotency_endpoints.py` / `test_redis_event_bus.py` / `test_ha_leader_failover.py` + 更新 `test_helm_templates.py`）全部通过；Redis 路径由 `fakeredis` mock，CI 无需起 Redis。
- `helm template infra/helm/ -f infra/helm/values.yaml` 渲染成功；7 个服务 Deployment 齐备，`agent-platform-api` 注入 `EVENT_BUS_BACKEND: redis`，`approval-service` + `mcp-gateway` 为一等公民条目。
- 端到端手测（双副本 + fakeredis lease）：leader 崩溃后 standby 在下一个 tick 窗口接管，无 schedule 双 fire；`POST /api/v1/agent-runs` 带 `Idempotency-Key` 重试回吐原 `run_id`；副本 A 发布的 run 事件被副本 B 的 WebSocket 订阅者收到。
- 静态检查：`ruff check` / `mypy --strict` 在改动文件上无新增告警。

## 4. 已知遗留 (Known Gaps)

- **`knowledge-api` 仍单副本**：`replicas: 1`，SQLite 单写。抬到 2 需先让 `pg_store` 成为默认元数据后端（P0-2 PostgreSQL 迁移未完成），否则两副本争写同一 SQLite 文件。HA 文档已标注此约束。
- **Redis 宕机 fail-open 而非 fail-closed**：`RedisIdempotencyStore` 在 Redis 不可达时把请求视为「未缓存」重执行，可用性优先于严格幂等。极端场景（Redis 长时间宕机 + 客户端重试）可能产生重复副作用，需配合客户端去重或后续引入持久化死信。
- **`RedisEventBus` listener 为线程**：每副本一个后台线程跑 `pubsub.get_message()` 循环，未用 asyncio 原生 Redis 客户端；高事件吞吐下线程切换开销与 `json.dumps` 序列化是潜在瓶颈，未做压测。
- **leader 选举 lease TTL 固定 15s / tick 间隔 30s**：failover 最坏窗口 ≈15s（lease 过期）+ 一次 tick 间隔。生产可调，但当前 values 未暴露为 Helm 参数。
- **无真实 Redis/Postgres 集成测试**：所有 HA 测试用 `fakeredis` + 进程内 lease 模拟，未在真实多副本 + 真 Redis + 真 Postgres 拓扑下验证（CI 无集群）。生产前需在 staging 做一次真 failover 演练。
- **`approval-service` / `mcp-gateway` 副本数抬到 2 但其状态后端未全部外部化**：HA 文档标注 `approval-service` 需 Postgres、`mcp-gateway` 需 Postgres 或 Redis，当前默认仍是 per-pod 存储；抬副本前需先确认对应后端已外部化（与 P0-2 PostgreSQL 迁移强耦合）。
- **无备份/恢复策略**：差距分析 §三 🔴「高可用（多副本/幂等/备份）」中的「备份」一项 R23 未触及；Postgres / Redis / 对象存储的定期备份与恢复 runbook 仍缺。

## 5. 下一步建议 (R24+ Candidates)

R20–R23 已把差距分析中 P0/P1 的多数项清零（见 `Docs/gap-analysis-2026-06-18.md` 收尾标注）。剩余候选按价值/工作量：

1. **PostgreSQL 成为默认元数据后端**（P0，高价值，中工作量）—— 让 `knowledge-api` / `approval-service` / `mcp-gateway` 真正 HA-safe 抬副本，打通 R23 多副本的最后一公里。
2. **对象存储深化**（R22 §5 降级项）—— Range + 分块流下载、写穿强一致 + 重试、object key ACL + 租户隔离、历史对象迁移脚本、MinIO 分布式 / 托管 S3。
3. **真实中间件集成测试**（中价值，中工作量）—— 在 CI 起 Postgres+Redis+MinIO 容器，跑一次真多副本 leader failover + 真幂等重试 + 真跨副本事件投递，替代 `fakeredis` 模拟。
4. **限流 + 熔断 + 工具健康检查**（P1-7/P1-8 剩余）—— `SlidingWindowLimiter` Redis 后端已就位，补 `ToolSpec` 的 `timeout_ms` / `retry_policy` / `health_status` 主动探活与熔断。
5. **备份与恢复 runbook**（P2 生产化）—— Postgres `pg_dump` 定时、Redis RDB/AOF、MinIO bucket 复制，配套 restore 演练脚本。
6. **LLM Trace（Langfuse/LangSmith）+ SSO/OIDC**（P2 治理深度）—— 差距分析 §三 🔴 两项，未在本轮覆盖。

建议 R24 主线接 1+3：Postgres 默认化 + 真集成测试，把 R23 的「文档化 HA」推到「CI 验证 HA」，再开 2/4/5/6 做对象存储深化与治理深度。
