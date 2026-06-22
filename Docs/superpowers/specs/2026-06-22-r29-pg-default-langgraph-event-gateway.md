# R29 — PG Defaulting + LangGraph Real Node Exec + Event-Gateway (2026-06-22)

目标：把 R24 收尾反复标记为「最高优先级」的 **PG 默认化**主线落地，同时闭合 spec §9 的「event-gateway 独立部署单元」与 spec P3 §3/§4 的「LangGraph v1 节点真正执行 LLM + MCP 工具」两条历史遗留。三条线正交、互不阻塞，分三个并行 worktree（R29-A / R29-B / R29-C）推进，最后在 worktree-4 合并、worktree-5 收尾推送。

## 背景

R24 收尾结论（见 `Docs/gap-analysis-2026-06-18.md` §7.2）把 **P0-2 PostgreSQL 迁移**列为 R25 最高优先级候选——R23 HA 文档与 R24 收尾均明确 `pg_store` 默认化是抬 `knowledge-api` / `approval-service` 副本的前置。R25–R28 轮次先后铺了可观测埋点（R25）、reranker + RCA 收敛深度（R26）、Kafka 真接线 + Neo4j 拓扑收敛 + 6 因子排序（R27）、真中间件全量回归冒烟 + 2 个被 skip 掩盖的缺陷修复（R28）。R29 终于动 PG 默认化主线，并顺手把 R27 留下的「rca-agent 进程内嵌 Kafka consumer」拆成独立 `event-gateway` 服务，同时让 LangGraph v1 节点从「假执行」变成真执行。

R28 基线：**1530 passed / 6 skipped / 0 failed**（真实 PG + MinIO 中间件，`TEST_POSTGRES_URL` 已设），`master` HEAD = `9394f58`。

## 三线总览

| 线 | worktree | 主题 | 闭合的差距条目 |
| --- | --- | --- | --- |
| **R29-A** | `wf_f5edeec9-598-1` | PG 默认化（`DATABASE_URL` 默认 + 启动日志 + helm `DATABASE_URL`） | §7.1 P0-2 PG 迁移 🔴 → ✅ |
| **R29-B** | `wf_f5edeec9-598-2` | LangGraph v1 节点真调用 LLM + MCP 工具 | §7.1 P2 LangGraph v1 深度集成 🔴 → ✅（执行层） |
| **R29-C** | `wf_f5edeec9-598-3` | event-gateway 独立服务（rca-agent 摘除 Kafka lifespan + 部署清单） | §三 §9 `event-gateway` 部署单元 ✅；§7.1 P2 Kafka 告警流（consumer 独立化）✅ |
| 合并 | `wf_f5edeec9-598-4` | 三线 merge 进同一分支 | 3 个 merge commit |
| 收尾 | `wf_f5edeec9-598-5` | 推送 origin/master + spec + gap-analysis | 本文档 |

---

## R29-A — PostgreSQL 默认化

### 目标

让一个 fresh checkout / `helm install` **默认就跑在 PG 上**，而不是 silent SQLite；同时给 operator 一个 `kubectl logs` 可见的 backend 选择日志，并在回落 SQLite 时发一次性 deprecation 警告。这把 R24 以来反复标记的 P0-2 从「代码闭环」推到「默认 PG 闭环」。

### 改动

**1. 四个 `build_*_store()` 工厂回落 SQLite 时发一次性 deprecation 警告**

`services/knowledge-api/src/ai_employee/knowledge_api/pg_store.py`、`services/rca-agent/src/ai_employee/rca_agent/pg_store.py`、`services/agent-platform-api/src/ai_employee/agent_platform_api/pg_run_store.py`、`services/approval-service/src/ai_employee/approval_service/store.py` 各加模块级 `_WARNED_FALLBACK = False` 节流标志。当 `DATABASE_URL` 未设、工厂选 SQLite 路径时，`global _WARNED_FALLBACK` 置 True 并 `_LOG.warning(...)`，整个进程只发一次。PG 路径不告警。

**2. 四个 `create_app` 启动时记录实际 wired 的 backend**

`knowledge-api` / `rca-agent` / `agent-platform-api` / `approval-service` 的 `create_app()` 在 store 构造后调 `detect_backend(os.getenv("DATABASE_URL", ""))`，按 `.name.lower() == "postgres"` 打 `"postgresql"` 否则 `"sqlite"`，`_LOG.info("<svc> create_app: using %s:// storage backend", backend_label)`。operator 一眼可见运行时选择，无需插桩。

**3. helm chart 默认注入 `DATABASE_URL` 指向集群内 PG**

`infra/helm/values.yaml` 加 `global.databaseUrl: "postgresql://ai-employee:ai-employee@postgres:5432/ai-employee"`；`infra/helm/templates/deployment.yaml` 把该值作为容器 env 注入四个 PG-backed 服务。`--set global.databaseUrl=...` 可覆盖到托管 PG（RDS / Cloud SQL / …）；留空回落 SQLite（dev/test）。

**4. `.env.example` 默认 `DATABASE_URL` 指向本地 docker-compose PG**

`.env.example` 顶部 `DATABASE_URL=postgresql://ai_employee:ai_employee@localhost:5432/ai_employee`，注释说明留空 / `sqlite:///...` → SQLite 模式（dev/test 兼容）。fresh checkout `docker compose up` 后即跑在 PG 上。

### 测试

`tests/test_r29_pg_defaulting.py`（560 行）pin 五条用户面契约：

1. `build_*_store()` 在 `DATABASE_URL` 指向 PG 时选 PG 后端、未设时回落 SQLite（保留 dev/test 行为）。
2. `DATABASE_URL` 未设时工厂发一次性 deprecation 警告（进程级节流）。
3. 每个 `create_app` 记录实际 wired 的 backend（`caplog` 断言 `postgresql` / `sqlite`）。
4. helm chart 默认向四个 PG-backed 服务注入 `DATABASE_URL`（`test_helm_templates.py` 加断言）。
5. `.env.example` 默认 `DATABASE_URL` 指向本地 PG DSN（`grep` 断言，fresh checkout 一步到 PG）。

提交链（5 个 TDD 子项 + 1 merge）：
| SHA | 标题 |
| --- | --- |
| `99a0ec0` | `feat(r29-a.1): add failing tests for PG defaulting + startup logs + helm DATABASE_URL` |
| `01f64cf` | `feat(r29-a.2): warn once when build_*_store falls back to SQLite` |
| `de69b2e` | `feat(r29-a.3): log storage backend at create_app startup` |
| `8929e02` | `feat(r29-a.4): helm chart defaults DATABASE_URL to cluster PG` |
| `1df593a` | `feat(r29-a.5): default DATABASE_URL to local PG in .env.example` |
| `8461d64` | `merge: R29-A PG defaulting (DATABASE_URL default + startup logs + helm DATABASE_URL)` |

---

## R29-B — LangGraph v1 节点真执行 LLM + MCP 工具

### 目标

pre-R29 的 `LangGraphRuntime` 产出的是「plan only」假节点语义——`_node_run_started` 从不调 `LlmClient.chat`，`_node_tool_plan` 从不调 `mcp_client.invoke_tool`，run 输出永远不反映任何模型结果。spec P3 §3/§4 要求 LangGraph 节点真正执行：run_started 调 LLM 把模型内容写进 `run.output.summary`；tool_plan 遍历 `template.tool_names` 逐个 `invoke_tool`，成功 → `status="completed"` + 写 `PlatformToolCallLogStore` 行，失败 → `status="failed"` + 带 `error_code`；approval_required 仍对 HITL 模板 pause；completed 仍对只读模板标 `completed`。

### 改动

`services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py`（+~400 行）：

- 加 `_LlmClientProtocol`（`chat`）+ `_McpClientProtocol`（`invoke_tool`）两个 `Protocol`，DI 面向接口。
- `_build_default_llm_client()` / `_build_default_mcp_client()` 懒构造默认 client（`LlmClient()` / 现有 mcp client），导入失败回落 `None`（保持老调用方 `build_langgraph_runtime()` 单例 / `RUNTIME_BACKEND` env 切换 / legacy `tests/test_langgraph_runtime.py` 的假 LLM / 无 mcp 路径继续工作）。
- `LangGraphRuntime.__init__` 接受可选 `llm_client` / `mcp_client`，测试注入 fake；懒属性 `_get_llm()` / `_get_mcp()` 首用构造。
- `_node_run_started`：调 `llm.chat(prompt)`，把 `response.content` 持久化进 `run.output.summary`。
- `_node_tool_plan`：遍历 `template.tool_names`，对非 approval-required 模板调 `mcp.invoke_tool(name, args)`；成功 → `status="completed"` + append `PlatformToolCallLogStore` 行；失败 → `status="failed"` + 行带 `error_code`。
- `_node_approval_required` / `_node_completed` 行为不变（HITL pause / 只读标 completed）。

### 测试

`tests/test_langgraph_runtime_node_execution.py`（341 行）用 `FakeLlmClient`（记录每次 `chat` 调用，可配置 `raise_error=LlmClientError` 验证失败处理）+ fake mcp client 验证：

- run_started 调 LLM、`run.output.summary` 反映模型内容。
- tool_plan 逐个 `invoke_tool`、成功行 `status="completed"` + `PlatformToolCallLogStore` 有行、失败行 `status="failed"` + `error_code`。
- approval-required 模板仍 pause（HITL 不回归）。
- 只读模板仍标 `completed`。
- DI 面可选——不注入时老调用方路径不破。

提交链（1 commit + 1 merge）：
| SHA | 标题 |
| --- | --- |
| `3a676f3` | `feat(r29-b): LangGraph v1 nodes invoke real LLM + MCP tools` |
| `27ca04c` | `merge: R29-B LangGraph v1 nodes invoke real LLM + MCP tools` |

---

## R29-C — event-gateway 独立服务

### 目标

pre-R29-C 的 rca-agent 进程在 lifespan 里内嵌 Kafka consumer（R27 接的）。rca-agent 一重启，告警流就断。spec §9 `event-gateway` 部署单元要求把 consumer 拆成独立服务，使：

- rca-agent 变成纯 HTTP consumer（`POST /api/v1/alarms/events`），Kafka-unaware。
- 告警流扛得住 rca-agent 重启。
- consumer 可独立扩缩容（gateway 对 rca-agent 是 stateless，Kafka consumer group 处理分区再均衡）。

### 改动

**1. 新服务 `services/event-gateway/`**

- `src/ai_employee/event_gateway/app.py`（184 行）：FastAPI app，`GET /health`（service=event-gateway + status=ok）+ `POST /api/v1/alarms/ingest`（公开 HTTP 告警入口，非 Kafka 源用，转发到 rca-agent）。lifespan 在 `KAFKA_ENABLED=1` 时 spawn 后台任务，定期 `AlarmForwarder.drain_batch()` 消费并把每条告警 HTTP POST 到 rca-agent；shutdown 关 consumer。
- `src/ai_employee/event_gateway/forwarder.py`（159 行）：`AlarmForwarder` 是 `KafkaAlarmConsumer`（解析 + 转换 Kafka payload）与 rca-agent HTTP 端点之间的胶水。`RcaClient` Protocol + `HttpRcaClient`（httpx，POST `<base_url>/api/v1/alarms/events`，带 `X-Internal-Token`）。负责把 Kafka on-wire schema（`alarm_id`/`site_id`/`alarm_code`/`severity`/`ts`）归一化到 rca-agent 的 `RawAlarmEvent` schema（加 `alarm_name`/`vendor`/`ne_id`/`start_time`）。
- `Dockerfile` + `README.md`：构建说明 + env 表（`KAFKA_ENABLED`/`KAFKA_BOOTSTRAP_SERVERS`/`KAFKA_ALARM_TOPIC`/`KAFKA_GROUP_ID`/`EVENT_GATEWAY_RCA_URL`/`INTERNAL_TOKEN`）。

**2. rca-agent 摘除 Kafka lifespan**

`services/rca-agent/src/ai_employee/rca_agent/app.py` 删掉 R27 的 `build_alarm_consumer()` + `_poll_loop()` 后台任务整块，lifespan 退化成 `yield`（纯 HTTP consumer）。同时加 R29-A 的 backend 启动日志。

**3. 部署清单**

- `infra/k8s/event-gateway.yaml`（72 行）：Deployment + Service（port 8060）+ PDB。
- `infra/helm/values.yaml`：`services.event-gateway`（enabled / pdb minAvailable 1 / port 8060 / replicas 2 / env: `KAFKA_BOOTSTRAP_SERVERS=kafka:9092` / `KAFKA_ALARM_TOPIC=alarms` / `KAFKA_GROUP_ID=event-gateway` / `EVENT_GATEWAY_RCA_URL=http://rca-agent:8020`）。
- `infra/docker-compose/compose.yml`：加 event-gateway 服务。
- `pyproject.toml` + `pytest.ini`：注册 event-gateway 的 src 到 pythonpath + setuptools。

### 测试

`tests/test_event_gateway.py`（283 行）pin 契约：

- `/health` 报 `service=event-gateway` + `status=ok`。
- `POST /api/v1/alarms/ingest` 接告警并转发到 rca-agent（`RcaClient` fake 注入，断言转发 payload + 返回 rca-agent 响应）。
- lifespan 在 `KAFKA_ENABLED=1` 时 spawn consumer（`drain_batch` 被 fake，断言调用）。
- 畸形消息被 drop（不崩 loop）。
- **回归测试**：rca-agent lifespan 不再构造 Kafka consumer（R27 wiring 摘除证明）。

提交链（4 个 TDD 子项 + 1 merge）：
| SHA | 标题 |
| --- | --- |
| `1388fd5` | `feat(r29-c.1): add failing test_event_gateway for independent event-gateway` |
| `68a0af7` | `feat(r29-c.2): scaffold event-gateway service with app + forwarder` |
| `e06f4c9` | `feat(r29-c.3): remove kafka lifespan from rca-agent (moved to event-gateway)` |
| `0a6ecff` | `feat(r29-c.4): wire event-gateway deploy manifests + helm values` |
| `ef49134` | `merge: R29-C event-gateway service (scaffold + rca-agent kafka removal + deploy manifests)` |

---

## 测试矩阵

### 全量 pytest（真 PG fallback 模式）

```
python -m pytest tests/ --ignore=tests/test_local_ci.py
```

R29 三线合并后在 in-memory fallback 模式（`TEST_POSTGRES_URL` 未设）跑全量：**0 regression**。R29 新增 3 个测试文件共 ~1184 行（`test_r29_pg_defaulting.py` 560 + `test_langgraph_runtime_node_execution.py` 341 + `test_event_gateway.py` 283），全部 green；老测试不破。

### 全量 pytest（真 Postgres）

设 `TEST_POSTGRES_URL` 后跑全量：**0 regression**。R29-A 的 PG 默认化在真 PG 腿下验证 `build_*_store()` 选 PG 后端、`create_app` 打 `postgresql://` 日志、helm 注入 `DATABASE_URL`。

### ruff lint 全仓

```
ruff check .
```

R29 改动文件全部 clean；全仓 pre-existing 61 个 ruff 错误属历史技术债（R28 已记录），R29 未引入新违规。

### 合并冲突

三线在 worktree-4 合并时无冲突——R29-A 改 `pg_store.py` / `app.py` / helm values / `.env.example`，R29-B 改 `langgraph_runtime.py`，R29-C 新建 `services/event-gateway/` + 改 rca-agent `app.py` 的不同区域 + 加 k8s manifest。唯一交叠是 rca-agent `app.py`（R29-A 加 backend 日志、R29-C 删 Kafka lifespan），手工合并 resolve。

## 改动面汇总

```
25 files changed, 2365 insertions(+), 111 deletions(-)
```

新文件：`services/event-gateway/`（Dockerfile / README / `__init__.py` / `app.py` / `forwarder.py`）、`infra/k8s/event-gateway.yaml`、3 个测试文件。改动文件：4 个 `pg_store.py` / `store.py`、4 个 `app.py`、`helm/values.yaml` + `templates/deployment.yaml`、`docker-compose/compose.yml`、`.env.example`、`pyproject.toml`、`pytest.ini`、`langgraph_runtime.py`、`pg_run_store.py`、`test_helm_templates.py`。

## Commit 列表

| SHA | 标题 |
| --- | --- |
| `3a676f3` | `feat(r29-b): LangGraph v1 nodes invoke real LLM + MCP tools` |
| `1388fd5` | `feat(r29-c.1): add failing test_event_gateway for independent event-gateway` |
| `68a0af7` | `feat(r29-c.2): scaffold event-gateway service with app + forwarder` |
| `e06f4c9` | `feat(r29-c.3): remove kafka lifespan from rca-agent (moved to event-gateway)` |
| `0a6ecff` | `feat(r29-c.4): wire event-gateway deploy manifests + helm values` |
| `99a0ec0` | `feat(r29-a.1): add failing tests for PG defaulting + startup logs + helm DATABASE_URL` |
| `01f64cf` | `feat(r29-a.2): warn once when build_*_store falls back to SQLite` |
| `de69b2e` | `feat(r29-a.3): log storage backend at create_app startup` |
| `8929e02` | `feat(r29-a.4): helm chart defaults DATABASE_URL to cluster PG` |
| `1df593a` | `feat(r29-a.5): default DATABASE_URL to local PG in .env.example` |
| `8461d64` | `merge: R29-A PG defaulting (DATABASE_URL default + startup logs + helm DATABASE_URL)` |
| `27ca04c` | `merge: R29-B LangGraph v1 nodes invoke real LLM + MCP tools` |
| `ef49134` | `merge: R29-C event-gateway service (scaffold + rca-agent kafka removal + deploy manifests)` |

13 commits（10 feat + 3 merge）。

## 推送结果

```
git push origin worktree-wf_f5edeec9-598-4:master
9394f58..ef49134  worktree-wf_f5edeec9-598-4 -> master   (fast-forward, 无 SSL 错误)
```

`origin/master` 已更新到 `ef49134`，本地 `master` 同步。

## 部署就绪度结论

**就绪（GO）**。理由：

1. **P0-2 PG 迁移从「代码闭环」推到「默认 PG 闭环」**：fresh checkout `docker compose up` + `helm install` 默认跑 PG，operator `kubectl logs` 可见 backend 选择，回落 SQLite 有一次性 deprecation 警告。R23/R24 两轮收尾反复标记的 HA 悬空（PG 默认化是抬 `knowledge-api`/`approval-service` 副本的前置）至此解除。
2. **LangGraph v1 从「假执行」推到「真执行」**：run_started 调 LLM、tool_plan 调 mcp `invoke_tool` + 写 `PlatformToolCallLogStore`、失败带 `error_code`、HITL/只读行为不回归。spec P3 §3/§4 执行层闭合。
3. **event-gateway 独立化**：rca-agent 摘除 Kafka lifespan 变纯 HTTP consumer，告警流扛得住 rca-agent 重启，consumer 可独立扩缩容（Kafka consumer group 再均衡）。spec §9 `event-gateway` 部署单元闭合。
4. **三线正交、向后兼容**：DI 面可选（`llm_client`/`mcp_client`/`rca_client` 注入），老调用方路径不破；env 门控（`KAFKA_ENABLED`/`DATABASE_URL`）保留 dev/test fallback；新字段可选。
5. **0 regression + lint 无新违规**：in-memory fallback 模式 + 真 PG 模式全量 green，R29 改动文件全部 clean。

**已知遗留（不阻塞）**：
- 全仓 61 个 pre-existing ruff 错误（R28 已记录，跨 ~15 个无关文件，建议后续单开一轮 `chore: ruff --fix`）。
- 6 个 skip 测试（R28 已记录：R27 `_SyncAdapter` asyncio 线程 ×2、Windows WAL ×1、`POSTGRES_TEST_DSN` 未设 ×2、故意 skip 行为验证 ×1）——R29-C 把 rca-agent 的 Kafka consumer 摘到 event-gateway 后，R27 `_SyncAdapter` 的两个 skip 是否仍相关待 R30 复核。
- event-gateway 的 Kafka consumer 在真 Kafka 下的 live 集成测试待 R30（本轮用 fake `drain_batch` 验证接线，未开真 broker）。
- PG 默认化只改了「默认值 + 日志 + 警告」，迁移脚本（`alembic upgrade head`）在 PG 模式下首次启动仍需 operator 手动跑（`.env.example` 注释已说明）。
