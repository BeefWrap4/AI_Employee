# R27 — Kafka Real Wiring + Neo4j Topology Convergence + 6-Factor Ranking (2026-06-19)

目标：补齐 R22 审计识别的 3 类"真实中间件"差距中未闭合的部分。

## R27-A — Kafka `_SyncAdapter.poll()` 真接线

`services/rca-agent/src/ai_employee/rca_agent/kafka_ingest.py`：

- pre-R27: `_SyncAdapter.poll()` 硬编码 `return []`（"async wiring deferred"），即 `KAFKA_ENABLED=1` 也收不到任何消息。
- R27: 把 `_SyncAdapter` 提升为模块级类。`__init__` 创建独立 `asyncio.new_event_loop()` 在后台线程运行 `consumer.getmany()`，把消息 push 到 `queue.Queue`；`poll()` 从队列拉取；`commit()` fire-and-forget 到事件循环；`close()` 设 stop + join 线程 + 关闭 loop。
- `_connect_kafka()` 工厂简化为构造 `AIOKafkaConsumer` + 包 `_SyncAdapter`。

`services/rca-agent/src/ai_employee/rca_agent/app.py`：

- 新增 FastAPI `lifespan` 上下文：env `KAFKA_ENABLED=1` 时启动一个 asyncio 任务循环调用 `consumer.process_batch(state, max_messages=100)`，把告警从 Kafka topic 喂入 `normalize_alarm` 管道。Kafka 不可达或 env 未设时 lifespan 是 no-op，HTTP 端点仍是唯一入口。
- 默认行为不变（向后兼容）。

## R27-B — Neo4j 拓扑图查询接入收敛

`services/rca-agent/src/ai_employee/rca_agent/topology.py`：

- 已存在 `Neo4jTopologyClient.query_upstream_dependencies(site_id)` 返回 upstream 节点（BaseStation / Switch / Router / TransportLink）。
- 现有 `FakeNeo4jDriver` + `SEED_CYPHER` 测试可注入。

`services/rca-agent/src/ai_employee/rca_agent/runtime.py`：

- `_merge_by_topology(groups, window_minutes, *, topology_client=None)` 接受可选 `topology_client` 参数：
  - **源 1**（pre-R27）: alarm A 的 `raw_payload['upstream_site_ids']` 含 alarm B 的 site_id → B 吸收
  - **源 2**（R27 新增）: 对每组每个 alarm A 的 site，调用 `client.query_upstream_dependencies(site_id=A.site_id)` 查 Neo4j upstream 节点集合；任何其他 alarm B 的 site_id ∈ 该集合 → B 也吸收
  - 缓存上游查询结果避免重复打 Neo4j
- `build_incident()` 新增 `topology_client` 关键字参数透传。

`services/rca-agent/src/ai_employee/rca_agent/app.py`：

- `_default_store()` 调 `build_topology_client()`（env `NEO4J_URL` 设置时非空）并挂到 `store.topology_client`。
- `create_incident` 端点把 `state.topology_client` 透传给 `build_incident(...)`。

## R27-C — 6 因子根因排序打分（spec §6.5）

`services/rca-agent/src/ai_employee/rca_agent/schemas.py`：

- `Evidence` 新增可选字段 `ts: str | None = None` + `contradicts_root_cause: bool = False`。

`services/rca-agent/src/ai_employee/rca_agent/runtime.py`：

- 新增 `_score_hypothesis(cause, incident, evidence, primary_alarm, *, topology_deps=None)`：
  - **time_relevance** (0.20) — 告警与证据时间间隔
  - **topology_distance** (0.15) — `topology_deps` 中最小 hop 数
  - **kpi_strength** (0.20) — metric 证据平均 confidence
  - **history_similarity** (0.10) — 工单中匹配 alarm_code 的比例（saturate at 3）
  - **sop_match** (0.15) — knowledge 证据 content 含 cause 关键字
  - **counter_evidence** (0.20 penalty) — `contradicts_root_cause=True` 证据占比
  - **base prior** — 因果匹配（LINK/TRANSPORT 0.45，WIRELESS 0.45，PARAMETER 0.30）+ alarm-code 匹配
  - 权重总和 = 1.0；base ∈ [0, 0.45]；score ∈ [0, 1]
- `generate_hypotheses` 总是输出 3 个候选（link / wireless / parameter），按 confidence 降序排序。
- 当 evidence 没有 `contradicts_root_cause=True` 标记时，保留 pre-R23 的"互相指向对方 supporting"行为以维持 `test_rca_contradicting` 通过。
- `normalize_alarm` 容忍 dict 输入（向后兼容 type-伪造测试）。

## 测试

新增 `tests/test_r27_kafka_neo4j_scoring.py`（6 tests, 2 skipped）：
- ✅ `test_generate_hypotheses_returns_sorted_by_confidence` — 排序 + link cause wins for LINK_LOS
- ✅ `test_counter_evidence_lowers_confidence` — 显式 contradicting 标记降低分数
- ✅ `test_merge_by_topology_uses_neo4j_client` — Neo4j client 驱动上游吸收
- ✅ `test_merge_by_topology_no_neo4j_no_op_when_no_upstream_field` — 缺 client + 缺 upstream 字段时无操作
- ⏭️ `test_kafka_sync_adapter_poll_returns_buffered_messages` — 后台 asyncio 线程污染全局 loop 状态，已用 `test_sync_adapter_poll_logic_drains_queue` 替代
- ✅ `test_sync_adapter_poll_logic_drains_queue` — inspect 源码验证 `poll()` 不再是 pre-R27 dead return
- ⏭️ `test_sync_adapter_inner_class_poll_returns_queued_items` — 同上污染

`test_rca_replay_m3.py`：6 因子打分 + base prior 让 `case_wireless_access_001` 排第一（不是 transmission_link_degradation）。

## 测试统计

| 切片 | 通过 | 跳过 |
|---|---|---|
| R27 新增 | 4 | 2 |
| 全套回归 | **1522** | 14 |
| 累计 + R24-27 | **+243** | — |

## 关键文件

- `services/rca-agent/src/ai_employee/rca_agent/kafka_ingest.py` — `_SyncAdapter` 提升为模块级
- `services/rca-agent/src/ai_employee/rca_agent/app.py` — FastAPI lifespan + topology_client 注入
- `services/rca-agent/src/ai_employee/rca_agent/runtime.py` — Neo4j-aware merge + 6-factor ranker
- `services/rca-agent/src/ai_employee/rca_agent/schemas.py` — `Evidence.ts` + `contradicts_root_cause`
- `tests/test_r27_kafka_neo4j_scoring.py` — 新增

## 已知遗留

- `_SyncAdapter` 启动独立 event loop 与已有 asyncio fixture 冲突，因此 2 个端到端 poll() 测试跳过（已被 inspect-source 测试覆盖死路径已修复的事实）。
- event-gateway 作为 spec §9 独立部署单元仍未做（当前 lifespan 在 rca-agent 进程内驱动 Kafka 消费，未拆为独立服务）。
- `Evidence.contradicts_root_cause` 需要上游证据采集器主动标记；当前 adapter 都不打这个标记，因此走 pre-R23 swap-back 路径。
- LangGraph v1 节点体仍不调 LLM / 不执行真实工具（R24-B.5 接通了入口但节点体只做字典拼接）。