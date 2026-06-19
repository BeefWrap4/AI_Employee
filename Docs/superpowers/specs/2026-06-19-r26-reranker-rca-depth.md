# R26 — Reranker Recall Window + RCA Convergence Depth (2026-06-19)

目标：补齐 R24 Recon 审计中识别的两项深度缺陷。

## R26-A — Reranker 召回窗口加宽

`services/knowledge-api/src/ai_employee/knowledge_api/retrieval.py`：
- Pre-R26: fused 后 `sorted(...)[:top_k]` 才送 reranker（默认 top_k=3），重排窗口过窄，cross-encoder / Stub 无意义信号可分。
- R26: 引入 `recall_window = min(50, max(top_k * 5, top_k))`，fused 阶段保留更宽候选，reranker 收窄到 top_k。这是 spec §5.4 二阶段重排的标准做法。
- 向后兼容：top_k=3 → recall=15；top_k=10 → recall=50；top_k=20 → recall=50（capped）。

## R26-B — RCA 收敛深度参数暴露

`services/rca-agent/src/ai_employee/rca_agent/schemas.py:IncidentBuildRequest`：
- 新增 `topology_window_minutes: int = Field(default=0, ge=0, le=240)` —— 0 表示关闭拓扑规则（pre-R26 默认）
- 新增 `parent_child_lag_seconds: int = Field(default=300, ge=0, le=3600)` —— 默认 5 分钟
- pre-R26 这两个参数被硬编码为 `topology_window_minutes=0` / `parent_child_lag_seconds=300`，API 调用方无法启用拓扑收敛或调整父子 lag 阈值

`services/rca-agent/src/ai_employee/rca_agent/app.py:create_incident`：
- 把两个新字段透传给 `build_incident(state, payload.alarms, payload.time_window_minutes, topology_window_minutes=..., parent_child_lag_seconds=...)`

## 测试

新增 `tests/test_r26_reranker_rca_depth.py`（6 tests）：
- `test_retrieval_passes_wide_candidate_window_to_reranker` — 通过 inspect 源码验证 `recall_window` 存在且 `rerank` 在其切片之后调用
- `test_built_in_stub_reranker_increases_candidate_count` — StubReranker 接受任意数量候选
- `test_incident_build_request_accepts_topology_window` — 新参数被接受并触发拓扑收敛
- `test_incident_build_request_defaults_preserve_compat` — 缺省仍工作
- `test_incident_build_rejects_zero_alarms` — alarm 列表非空约束
- `test_topology_window_negative_rejected` — 负值 422

## 测试统计

- R26 新增：6 tests
- 全套 (master + R25 + R26)：**1517 passed, 12 skipped, 0 failed**

## 已知遗留

- `_merge_by_topology` 仍仅基于 `site_id ∈ upstream_site_ids` 字符串比对，未真正接 Neo4j 图查询（spec §6.2 拓扑维度需迭代）
- 反证 (`contradicting_evidence_ids`) 仍是结构化填充（h1 反证 = h2 支持集），非内容驱动真反证
- 根因排序 6 大因子（时间/拓扑距离/KPI 强度/历史相似度/SOP 命中/反证数）仍无打分逻辑，confidence 是写死常量
- Kafka 告警流真接线 + event-gateway 独立服务仍未做（spec §9 部署单元 + spec §3 告警接入）；当前 `_SyncAdapter.poll() return []` 是死路径
- LangGraph runtime 节点内仍无 LLM 调用 / 无真实工具执行；R24-B.5 接通了入口但节点体只做字典拼接

## 关键文件

- `services/knowledge-api/src/ai_employee/knowledge_api/retrieval.py` (recall_window)
- `services/rca-agent/src/ai_employee/rca_agent/schemas.py` (IncidentBuildRequest 新字段)
- `services/rca-agent/src/ai_employee/rca_agent/app.py` (create_incident 透传)
- `tests/test_r26_reranker_rca_depth.py` (新增)