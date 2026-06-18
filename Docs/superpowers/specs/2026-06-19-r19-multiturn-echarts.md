# Spec — R19 收尾：多轮追问 + ECharts 趋势图

日期：2026-06-19
主题：R19 阶段交付总览——LLM 多轮上下文注入、ECharts 趋势数据端点、SSE 图表事件扩展
适用范围：`services/knowledge-api`、`packages/llm-gateway`、`tests/`

## 1. 目标 (Goal)

R18 已上线 RCA 报告完整性 / 安全策略 / 工具调用正确性三类离线评测，但 `chat/query` 仍然是「单轮问答」形态：

- 追问 (followup) 时 LLM 看不到上文，只能基于当前问题召回，无法消解代词 / 指代。
- 缺乏可被前端 ECharts 直接消费的结构化趋势数据接口，前端必须自己再拼数据 + 拼 option。
- 流式接口 `/chat/query/stream` 不携带任何图表元信息，前端 SSE 客户端无法在不解析文本的情况下挂图。

R19 在不破坏现有 RAG 闭环（M1）、不引入新数据库依赖的前提下，补齐「多轮 + 图表」最后一公里，使前端在不改协议的前提下能：

1. 让 LLM 在第 2 轮及以后看到「前序 chunk + 前序答案」摘要。
2. 通过专用端点 `POST /api/v1/chat/echarts` 直接拿到 ECharts `option` dict（`xAxis` / `yAxis` / `series`）。
3. 在 SSE 流末尾拿到一个轻量 `event: chart` 帧，包含 `chart_id` + `schema_url`，前端按需再拉 `echarts` 端点拿完整数据。

## 2. 实现清单 (Deliverables)

### 2.1 后端

| 模块 | 文件 | 变更要点 |
| --- | --- | --- |
| `packages/llm-gateway` | `src/ai_employee/llm_gateway/prompt.py` | 新增 `MULTITURN_CONTEXT_HEADER` 常量，供 system prompt 注入「前序上下文」段；保留既有 `context_str` 注入点不变。 |
| `services/knowledge-api` | `src/ai_employee/knowledge_api/app.py` | `POST /api/v1/chat/query` 改为：当请求携带 `session_id` 且历史 ≥ 1 轮时，从 `SQLiteStore` 取出最近若干轮的 `chunks` + `answer`，拼成 `prior_context_str` 并塞到 `context_str` 之前；新增 `POST /api/v1/chat/echarts` 端点；`/chat/query/stream` 在 `done` 事件前多发一帧 `event: chart`。 |
| `services/knowledge-api` | `src/ai_employee/knowledge_api/echarts.py` (新) | 抽象 `EChartsAggregator` + 两个内置实现 `AlarmAggregator`（基于 rca-agent `AlarmEvent`）/ `KpiAggregator`（基于 `KpiPoint`），按 `metric` + `window_minutes` 做时间桶聚合并组装 ECharts option。 |
| `services/knowledge-api` | `src/ai_employee/knowledge_api/schemas.py` | 新增 `EChartsRequest` / `EChartsResponse` Pydantic 模型；`ChartEvent` SSE 帧结构 (`chart_id`, `schema_url`, `metric`)。 |

### 2.2 前端

本轮**无新增前端代码**——R19 是后端能力补齐，ECharts 渲染与 SSE 事件处理留给前端 agent 在 R20 跟进消费。前端 manifest (`apps/web-portal`) 无任何 diff。

### 2.3 测试

| 测试文件 | 用例数 | 覆盖点 |
| --- | --- | --- |
| `tests/test_multiturn_context.py` (新) | 2 | 追问时 `context_str` 含前序 chunks+answer 摘要；首轮不注入空 prefix。 |
| `tests/test_echarts_endpoint.py` (新) | 3 | 端点返回 `xAxis` / `yAxis` / `series` 三件套；`metric=kpi_*` 时走 `KpiAggregator`；无数据时返回 404。 |
| `tests/test_sse_chart_event.py` (新) | 2 | `/chat/query/stream` 末帧含 `event: chart` + `chart_id` + `schema_url`；可通过环境变量 / 请求头关掉图表事件。 |

合计 +7 个新测试，均使用 `TestClient` + 注入式 fake aggregator / fake LlmClient，无需真实 LLM / DB。

## 3. 测试结果 (Test Results)

- 全量 R17/R18 测试保持通过（`test_rca_report_eval`、`test_safety_policy_eval`、`test_tool_call_correctness` 等无回归）。
- 新增 7 个 R19 测试在 `pytest -q tests/test_multiturn_context.py tests/test_echarts_endpoint.py tests/test_sse_chart_event.py` 下全部通过。
- 端到端手测（curl + SSE 客户端）：`POST /api/v1/chat/query` 携带 `session_id` 第 2 次请求时，`LlmClient.chat(messages)` 的 system 段首部出现「Prior context from earlier turns」块；`POST /api/v1/chat/echarts` 在无 metric 数据时返回 404 + `{"detail": "no data for metric=... window=..."}`；`/chat/query/stream` 末帧 JSON 含 `chart_id` 与 `schema_url=/api/v1/chat/echarts?...`。
- 静态检查：`ruff check` / `mypy --strict` 在改动文件上无新增告警。

## 4. 已知遗留 (Known Gaps)

- **ECharts 聚合数据源有限**：目前只覆盖 `AlarmEvent`（rca-agent SQLite store）与 `KpiPoint`（influx 适配器）两类；Neo4j 拓扑 / Postgres baseline 表尚无对应 aggregator。
- **多轮上下文窗口硬编码**：当前固定取最近 3 轮，每轮截断 800 字符，未做 token 预算控制，LLM 侧 `prompt_tokens` 会随追问轮数线性增长。
- **SSE 图表事件可关闭但无开关文档**：通过请求头 `X-Disable-Chart-Event: 1` 关掉，前端 manifest 未做对应 UI 暴露。
- **echarts 端点无租户隔离**：`session_id` 维度上未做 ACL 校验，跨租户 `session_id` 可拿到别人图表（与 M2.2 ACL 设计一致，待合并）。
- **没有 e2e Playwright 用例**：本轮测试全是单元/集成层，前端 ECharts 真实挂图待 R20 补 Playwright。

## 5. 下一步建议 (R20 Candidates)

按「价值/工作量」排序的 R20 候选：

1. **前端 ECharts 接入**（高价值，中工作量）—— 在 `apps/web-portal` 的 `/chat` 页面接入 `/api/v1/chat/echarts` 与 SSE `event: chart` 帧，新增 Playwright e2e 用例验证「问完一句话 → 看到图」。
2. **多轮上下文 token 预算控制**（中价值，低工作量）—— 在 `prompt.py` 引入 `MULTITURN_TOKEN_BUDGET` 配置 + 摘要回退（首句 + 实体），避免 prompt 膨胀。
3. **echarts 端点 ACL + 限流**（高价值，中工作量）—— 复用 M2.2 ACL 中间件 + R14 滑动窗口限流器，跨租户返回 403 / 429。
4. **Neo4j 拓扑图 aggregator**（中价值，中工作量）—— 新增 `TopologyAggregator`，把节点出入度时间序列折成 ECharts 双 Y 轴 option。
5. **多模态追问**（探索性）—— 把 R17-4 接入的 Qwen-VL-OCR 用于「截图追问」场景（用户截一段图问「这段是哪个 KPI 异常」），仍走同一多轮 prompt 通道。
6. **流式图表增量**（探索性）—— SSE 端点不再只发 `chart_id`，而是边聚边推 `chart.delta`，让前端 ECharts 实现「边问边画」。

建议 R20 主线接 1+2+3，前端闭环后再开 4/5/6。
