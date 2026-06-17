# Spec — M3 基站告警 RCA Agent 原型

日期：2026-06-17  
主题：M3 里程碑——告警标准化、Incident 收敛、上下文采集、RCA Runtime 状态机原型  
适用范围：`services/rca-agent`、`packages/common-schemas`、`infra/docker-compose`、`tests/rca-replay`

参考：`Docs/project-2-base-station-alarm-rca-agent-design-spec.md`（设计规格）、`Docs/ai-agent-telecom-projects-implementation-plan.md` §4（M3 模块拆分与工具接口）

## 1. 背景

M1/M2 已完成 RAG 知识底座（knowledge-api + ingestion-worker + eval-service + ACL + 降级 + db 错误码）。M3 进入阶段二：基站告警根因分析 Agent 原型。

M3 验收（实施计划 §6）：**incident 可生成证据池**。M4 才做根因候选真实推理、报告人审、历史回放评测。本轮 M3 搭建：

- 告警标准化 + Incident 收敛
- 5 个上下文采集工具（模拟数据 + 复用 knowledge-api）
- RCA Runtime 状态机（LangGraph 0.x，7 节点，Reason/Verify/Rank/Report 本轮 stub）
- API + PostgreSQL 持久化 + Celery 异步

## 2. 目标

- `services/rca-agent` 独立 FastAPI 服务（端口 8002），PostgreSQL 持久化 + Celery/Redis 异步。
- LangGraph 0.x 状态机驱动 Triage→Plan→Collect→Reason→Verify→Rank→Report 七节点。
- 5 工具：KPI/日志/拓扑/工单 用内置 fixtures；知识库工具复用 knowledge-api HTTP。
- 10 个 API 端点覆盖告警接入、Incident 构建、RCA run、证据池、报告、人审。
- docker-compose 加 postgres + redis。
- 端到端：上传告警 → 收敛 incident → 创建 run → 跑通 7 节点 → 产出证据池 + 报告骨架。

## 3. 非目标

- LLM 根因推理（M4）：Reason/Verify/Rank 本轮 stub。
- 历史回放评测（M4）：`tests/rca-replay/` 只放 fixtures，不做 Top-N 指标。
- 真实 Prometheus/InfluxDB/Neo4j/工单系统接入（M4+）。
- 高风险动作自动执行（永久非目标）。
- Alembic 迁移（M4/M5 引入；M3 用 CREATE TABLE IF NOT EXISTS）。
- Web 门户 RCA 页面（M5）。

## 4. 仓库与模块布局

```
AI_Employee/
├─ services/rca-agent/
│  └─ src/ai_employee/rca_agent/
│     ├─ __init__.py
│     ├─ app.py                 # FastAPI 入口
│     ├─ store.py               # PostgreSQL store（psycopg）
│     ├─ schemas.py             # Pydantic
│     ├─ normalizer.py          # 告警标准化
│     ├─ correlator.py          # Incident 收敛
│     ├─ tools/
│     │  ├─ __init__.py         # ToolRegistry
│     │  ├─ kpi.py
│     │  ├─ log.py
│     │  ├─ topology.py
│     │  ├─ knowledge.py        # 调 knowledge-api HTTP
│     │  └─ ticket.py
│     ├─ runtime/
│     │  ├─ __init__.py
│     │  ├─ graph.py            # LangGraph 状态机
│     │  ├─ state.py            # RCAState TypedDict
│     │  └─ nodes.py            # 7 节点实现
│     └─ tasks.py               # Celery 任务
├─ packages/common-schemas/src/ai_employee/common_schemas/
│  └─ rca.py                    # AlarmEvent/Incident/Evidence/Hypothesis 共享模型
├─ infra/docker-compose/compose.yml  # 修改：加 postgres + redis
├─ tests/rca-replay/
│  ├─ alarms.jsonl
│  ├─ kpi_fixtures.json
│  ├─ log_fixtures.json
│  ├─ topology_fixtures.json
│  └─ ticket_fixtures.json
├─ tests/
│  ├─ test_rca_normalizer.py
│  ├─ test_rca_correlator.py
│  ├─ test_rca_tools.py
│  ├─ test_rca_runtime.py
│  └─ test_rca_api.py
├─ pyproject.toml               # 加 langgraph/celery/psycopg/redis + ai_employee.rca_agent
└─ pytest.ini                   # 加 services/rca-agent/src
```

包注册：`pyproject.toml` `[tool.setuptools]` packages 加 `ai_employee.rca_agent`，package-dir 映射 `services/rca-agent/src/ai_employee/rca_agent`；dependencies 加 `langgraph>=0.2`、`celery>=5.3`、`psycopg[binary]>=3.1`、`redis>=5.0`。`pytest.ini` pythonpath 加 `services/rca-agent/src`。

约束：
- RCA 与 knowledge-api 解耦：知识库工具走 HTTP，不直连 SQLite。
- PostgreSQL store 用 `psycopg`，参数化 SQL，复用 M1 store 的装饰器风格。
- Celery 任务异步跑 LangGraph；API 只创建 run 记录 + 投递任务。
- 测试用 FakeRcaStore（内存）+ Celery eager + mock httpx，不依赖真实 PG/Redis。

## 5. 数据模型（PostgreSQL）

### 5.1 alarm_events

| 字段 | 类型 | 说明 |
|---|---|---|
| `alarm_event_id` | TEXT PK | `ae_001` |
| `incident_id` | TEXT NULL | 关联 incident（未收敛 NULL） |
| `alarm_code` | TEXT NOT NULL | 告警码 |
| `alarm_name` | TEXT NOT NULL | |
| `vendor` | TEXT | |
| `site_id` | TEXT NOT NULL | |
| `cell_id` | TEXT | |
| `ne_id` | TEXT | |
| `severity` | TEXT NOT NULL | critical/major/minor/warning |
| `start_time` | TIMESTAMPTZ NOT NULL | |
| `clear_time` | TIMESTAMPTZ | |
| `fingerprint` | TEXT NOT NULL | 去重指纹 |
| `raw_payload` | JSONB NOT NULL | |
| `created_at` | TIMESTAMPTZ NOT NULL | |

### 5.2 incidents

| 字段 | 类型 | 说明 |
|---|---|---|
| `incident_id` | TEXT PK | `inc_001` |
| `incident_no` | TEXT NOT NULL UNIQUE | 业务编号 |
| `title` | TEXT NOT NULL | |
| `status` | TEXT NOT NULL | open/analyzing/reviewed/closed |
| `severity` | TEXT NOT NULL | P0/P1/P2/P3 |
| `site_id` | TEXT NOT NULL | 主站点 |
| `start_time` | TIMESTAMPTZ NOT NULL | |
| `end_time` | TIMESTAMPTZ | |
| `summary` | TEXT | |
| `primary_alarm_event_id` | TEXT | 主告警 |
| `created_at` | TIMESTAMPTZ NOT NULL | |

### 5.3 evidence

| 字段 | 类型 | 说明 |
|---|---|---|
| `evidence_id` | TEXT PK | `e_001` |
| `incident_id` | TEXT NOT NULL | FK |
| `rca_run_id` | TEXT NULL | 关联 run |
| `source_type` | TEXT NOT NULL | metric/log/topology/kb/ticket |
| `source_ref` | TEXT | 来源引用 |
| `content` | TEXT NOT NULL | 证据摘要 |
| `raw_data` | JSONB NOT NULL | |
| `confidence` | REAL NOT NULL | |
| `created_at` | TIMESTAMPTZ NOT NULL | |

### 5.4 rca_runs

| 字段 | 类型 | 说明 |
|---|---|---|
| `rca_run_id` | TEXT PK | `run_001` |
| `incident_id` | TEXT NOT NULL | FK |
| `status` | TEXT NOT NULL | pending/running/completed/failed |
| `current_node` | TEXT | 当前节点 |
| `mode` | TEXT NOT NULL | auto_collect |
| `max_tool_calls` | INTEGER NOT NULL DEFAULT 20 | |
| `require_human_review` | BOOLEAN NOT NULL DEFAULT TRUE | |
| `evidence_count` | INTEGER NOT NULL DEFAULT 0 | |
| `hypothesis_count` | INTEGER NOT NULL DEFAULT 0 | |
| `error` | TEXT | |
| `trace_id` | TEXT NOT NULL | |
| `created_at` | TIMESTAMPTZ NOT NULL | |
| `updated_at` | TIMESTAMPTZ NOT NULL | |
| `completed_at` | TIMESTAMPTZ | |

### 5.5 rca_reports

| 字段 | 类型 | 说明 |
|---|---|---|
| `report_id` | TEXT PK | `rpt_001` |
| `rca_run_id` | TEXT NOT NULL | FK |
| `incident_id` | TEXT NOT NULL | |
| `report_md` | TEXT NOT NULL | Markdown |
| `hypotheses` | JSONB NOT NULL | 根因候选 |
| `final_root_cause` | TEXT | 人工确认 |
| `review_status` | TEXT NOT NULL DEFAULT 'pending' | pending/accepted/rejected |
| `reviewer` | TEXT | |
| `review_comment` | TEXT | |
| `created_at` | TIMESTAMPTZ NOT NULL | |

schema 迁移：`init_schema()` 执行 `CREATE TABLE IF NOT EXISTS`（psycopg）。无 Alembic。

## 6. 告警标准化与 Incident 收敛

### 6.1 告警标准化（normalizer.py）

```python
@dataclass
class AlarmEvent:
    alarm_event_id: str
    alarm_code: str
    alarm_name: str
    vendor: str
    site_id: str
    cell_id: str | None
    ne_id: str | None
    severity: str          # critical/major/minor/warning
    start_time: datetime
    clear_time: datetime | None
    fingerprint: str
    raw_payload: dict
```

`normalize(raw: dict) -> AlarmEvent`：
- 字段别名映射：`alarm_code` / `alarmCode` / `code`；`site_id` / `siteId` / `station_id`；等。
- severity 归一：`1/critical/Critical` → `critical`；`2/major` → `major`；`3/minor` → `minor`；`4/warning` → `warning`。
- fingerprint = `sha1(f"{alarm_code}|{site_id}|{ne_id}|{start_time_floor_5min}")`。5 分钟窗内同 code+site+ne 视为重复。

去重：`POST /alarms/events` 收到告警后算 fingerprint，DB 已存在同 fingerprint 且未 clear → 返回已存在 alarm_event_id（幂等）。

### 6.2 Incident 收敛（correlator.py）

收敛策略（spec §6.2）：
1. 时间窗口：start_time 前后 30 分钟内聚合。
2. 空间范围：同 site_id 优先；同 ne_id 次之；同传输 link_id（拓扑 fixture 推断）。
3. 主告警：severity 最高（critical > major > minor > warning）；同级取最早 start_time。
4. 伴随告警：同 incident 内非主告警。

`build_incidents(alarm_events, window_minutes=30) -> list[Incident]`：
- 按 site_id 分组
- 组内按 start_time 排序，滑动窗口聚合
- 每组生成 incident，标注 primary_alarm_event_id

### 6.3 API

- `POST /api/v1/alarms/events`：接收单条/批量原始告警，标准化 + 去重 + 入库，返回 alarm_event_id 列表 + normalized_count + deduped_count。
- `POST /api/v1/incidents/build`：从指定 alarm_event 集合（或全量未收敛告警）构建 incident，返回 incident_id + 主告警 + 伴随告警数量。

## 7. 工具层（5 工具 + ToolRegistry）

### 7.1 Tool 协议

```python
class Tool(Protocol):
    name: str
    source_type: str  # metric/log/topology/kb/ticket
    def invoke(self, **params) -> ToolResult: ...

@dataclass
class ToolResult:
    source_ref: str
    content: str
    raw_data: dict
    confidence: float
```

### 7.2 5 工具

| 工具 | name | source_type | 数据源 | invoke 参数 |
|---|---|---|---|---|
| `KpiTool` | `kpi_query` | `metric` | `tests/rca-replay/kpi_fixtures.json` | site_id, cell_id, time_window, metric_names |
| `LogTool` | `log_search` | `log` | `tests/rca-replay/log_fixtures.json` | ne_id, time_window, keywords |
| `TopologyTool` | `topology_query` | `topology` | `tests/rca-replay/topology_fixtures.json` | site_id, ne_id, link_id |
| `KnowledgeTool` | `kb_search` | `kb` | knowledge-api `/api/v1/chat/query` | alarm_code, symptom, query |
| `TicketTool` | `ticket_query` | `ticket` | `tests/rca-replay/ticket_fixtures.json` | site, vendor, symptom |

### 7.3 fixture 格式（KPI 示例）

```json
{
  "site_001": {
    "rrc_setup_failure_rate": {
      "series": [{"ts": "2025-05-01T10:00:00Z", "value": 12.5}],
      "anomaly_points": [{"ts": "...", "value": 45.2, "baseline": 8.0}],
      "missing": false
    }
  }
}
```

工具按 site_id/metric 查 fixture；命中返回 series+anomaly，未命中返回 `missing=true` + 低 confidence。

### 7.4 KnowledgeTool 容错

knowledge-api 不可达时返回 `confidence=0.0` + `content="knowledge api unavailable"`，不抛错（RCA 不因知识库故障中断）。

### 7.5 ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool): ...
    def get(self, name: str) -> Tool: ...
    def all(self) -> list[Tool]: ...
```

Runtime Collect 节点通过 registry 调用工具，结果转 Evidence 入库。

## 8. RCA Runtime（LangGraph 状态机）

### 8.1 RCAState

```python
class RCAState(TypedDict):
    incident_id: str
    rca_run_id: str
    trace_id: str
    current_node: str
    fault_type: str | None
    collect_plan: list[dict]
    evidence_ids: list[str]
    hypotheses: list[dict]
    verified_hypotheses: list[dict]
    ranked_hypotheses: list[dict]
    report_md: str
    error: str | None
    tool_call_count: int
    max_tool_calls: int
```

### 8.2 7 节点

| 节点 | M3 实现 | 转换 |
|---|---|---|
| `triage` | 规则：按主告警 alarm_code 分类（wireless_access / transmission / clock / device_down）→ fault_type | → plan |
| `plan` | 按 fault_type 选工具组合生成 collect_plan | → collect |
| `collect` | 遍历 collect_plan 调 ToolRegistry，结果转 Evidence 入库，受 max_tool_calls 限制 | → reason |
| `reason` | **stub**：基于 fault_type + evidence 生成 1-2 模板假设，绑定 evidence_ids | → verify |
| `verify` | **stub**：按 source_type 标注 supporting/contradicting evidence_ids | → rank |
| `rank` | **stub**：按 confidence 排序取 Top-3 | → report |
| `report` | 生成 Markdown 报告（事件摘要、影响范围、证据链、Top-N 假设、待确认项、引用来源） | → END |

### 8.3 LangGraph 图（graph.py）

```python
from langgraph.graph import StateGraph, END

def build_rca_graph(tools, store):
    g = StateGraph(RCAState)
    g.add_node("triage", triage_node)
    g.add_node("plan", plan_node)
    g.add_node("collect", collect_node)
    g.add_node("reason", reason_node)
    g.add_node("verify", verify_node)
    g.add_node("rank", rank_node)
    g.add_node("report", report_node)
    g.set_entry_point("triage")
    g.add_edge("triage", "plan")
    g.add_edge("plan", "collect")
    g.add_edge("collect", "reason")
    g.add_edge("reason", "verify")
    g.add_edge("verify", "rank")
    g.add_edge("rank", "report")
    g.add_edge("report", END)
    return g.compile()
```

线性图（M3 无条件分支；M4 加 need_more_evidence 回环）。

### 8.4 节点执行约定

- 每个节点：读 state → 执行 → 返回 state 增量（LangGraph 合并）。
- 节点执行后更新 `rca_runs.current_node` + `updated_at`。
- 节点异常 → state.error 设置 → 图终止 → run status=failed。
- collect 节点 tool_call_count 累加，超 max_tool_calls 停止采集。

### 8.5 Celery 任务（tasks.py）

```python
@celery.task
def run_rca_analysis(rca_run_id, incident_id, trace_id):
    store = RcaStore(...)
    tools = build_default_registry()
    graph = build_rca_graph(tools, store)
    initial_state = RCAState(...)
    store.update_run_status(rca_run_id, "running")
    try:
        final_state = graph.invoke(initial_state)
        store.update_run_status(rca_run_id, "completed", ...)
    except Exception as exc:
        store.update_run_status(rca_run_id, "failed", error=str(exc))
```

`POST /rca/runs` 创建 run（status=pending）→ 投递 Celery → 返回 run_id + trace_id。客户端轮询 `GET /rca/runs/{run_id}`。

## 9. API 表面

### 9.1 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/alarms/events` | 接收/回放原始告警（单条或批量） |
| `GET` | `/api/v1/alarms/events/{alarm_event_id}` | 查询单条标准化告警 |
| `POST` | `/api/v1/incidents/build` | 从告警集合构建 incident |
| `GET` | `/api/v1/incidents/{incident_id}` | 查询 incident 详情（含告警列表） |
| `POST` | `/api/v1/rca/runs` | 创建 RCA 分析（投递 Celery） |
| `GET` | `/api/v1/rca/runs/{run_id}` | 查询运行状态 |
| `GET` | `/api/v1/rca/runs/{run_id}/evidence` | 列出 run 的证据池 |
| `GET` | `/api/v1/rca/reports/{report_id}` | 查看 RCA 报告 |
| `POST` | `/api/v1/rca/reports/{report_id}/review` | 人工确认报告 |
| `GET` | `/health` | 健康检查 |

### 9.2 请求/响应示例

`POST /api/v1/alarms/events`（批量）：
```json
{
  "alarms": [
    {"alarm_code": "AL-25401", "alarm_name": "小区不可用", "site_id": "site_001",
     "vendor": "huawei", "severity": "critical", "start_time": "2025-05-01T10:02:00Z",
     "raw_payload": {}},
    {"alarm_code": "AL-25401", "site_id": "site_001", "severity": "1",
     "start_time": "2025-05-01T10:03:00Z", "raw_payload": {}}
  ]
}
```
响应：`{alarm_event_ids: ["ae_001"], normalized_count: 2, deduped_count: 1}`（第 2 条 5 分钟窗内重复去重）。

`POST /api/v1/rca/runs`：
```json
{"incident_id": "inc_001", "mode": "auto_collect", "max_tool_calls": 20, "require_human_review": true}
```
响应：`{rca_run_id: "run_001", incident_id: "inc_001", status: "pending", trace_id: "trace_run_001"}`

### 9.3 配置项

| 变量 | 默认 | 用途 |
|---|---|---|
| `RCA_DB_URL` | `postgresql://rca:rca@localhost:5432/rca` | PG 连接串 |
| `RCA_REDIS_URL` | `redis://localhost:6379/0` | Celery broker |
| `KNOWLEDGE_API_URL` | `http://127.0.0.1:8010` | knowledge-api 地址 |
| `RCA_FIXTURES_DIR` | `tests/rca-replay` | 工具 fixture 目录 |
| `RCA_RUN_TIMEOUT_S` | `300` | 单次 RCA run 超时 |

### 9.4 docker-compose（infra/docker-compose/compose.yml 追加）

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: rca
      POSTGRES_PASSWORD: rca
      POSTGRES_DB: rca
    ports: ["5432:5432"]
    volumes: ["rca_pg:/var/lib/postgresql/data"]
  redis:
    image: redis:7
    ports: ["6379:6379"]
volumes:
  rca_pg:
```

## 10. 测试策略

### 10.1 测试分层（mock PG/Redis）

| 文件 | 类型 | 覆盖 |
|---|---|---|
| `tests/test_rca_normalizer.py` | 单元 | 字段别名映射；severity 归一；fingerprint 5 分钟窗去重；缺失字段处理 |
| `tests/test_rca_correlator.py` | 单元 | 同 site 时间窗聚合；跨 site 不合并；主告警 severity 优先；同级取最早；伴随告警标注 |
| `tests/test_rca_tools.py` | 单元 | 5 工具 invoke 返回 ToolResult；fixture 命中/未命中；KnowledgeTool mock httpx + 不可达降级 confidence=0 |
| `tests/test_rca_runtime.py` | 集成 | LangGraph 7 节点跑通（FakeRcaStore）；Collect 产出 evidence；stub 输出结构正确；max_tool_calls 限制；节点异常 → failed |
| `tests/test_rca_api.py` | 集成 | FastAPI TestClient + FakeRcaStore + Celery eager：alarms 批量去重；incidents/build；rca/runs；evidence 列表；report review；/health |

### 10.2 fixtures（tests/rca-replay/）

- `alarms.jsonl`：10 条模拟告警（含同 site 5 分钟内重复、跨 site、不同 severity）
- `kpi_fixtures.json`：3 site × 4 metric，含 anomaly_points + missing
- `log_fixtures.json`：5 ne 日志片段 + 错误码
- `topology_fixtures.json`：3 site 上下游 + link_id
- `ticket_fixtures.json`：5 条历史工单

### 10.3 mock 策略

- **store**：`FakeRcaStore`（内存 dict），接口与 `RcaStore` 一致。
- **Celery**：`celery_app.conf.task_always_eager = True`（同步）。
- **KnowledgeTool**：mock `httpx.post` 返回固定 citation。
- **LangGraph**：真实跑（纯 Python，无外部依赖）。

### 10.4 端到端（可选，CI 标注）

- `tests/test_rca_e2e.py`：需 docker-compose PG+Redis + knowledge-api。标注 `@pytest.mark.e2e`，默认跳过。

### 10.5 不在 M3 测试范围

- 真实 Prometheus/InfluxDB/Neo4j 接入。
- LLM 根因推理质量。
- 告警风暴压测。
- 工单系统真实回写。

## 11. 验收

- `python -m pytest` 全部通过（193 现有 + M3 新增）。
- 端到端：上传 10 条告警 → 收敛为 incident → 创建 RCA run → 7 节点跑通 → 证据池 ≥ 3 条 → 报告 Markdown 含事件摘要 + Top-N 假设 + 待确认项。
- 告警去重：5 分钟窗内同 code+site+ne 重复告警 → deduped_count 正确。
- Incident 收敛：同 site 30 分钟内多告警 → 1 个 incident + 主告警正确标注。
- KnowledgeTool 降级：knowledge-api 不可达 → confidence=0 + 不中断 RCA。
- 节点异常：mock collect 抛错 → run status=failed + error 字段。

## 12. 实施拆分（建议执行顺序）

1. `common_schemas.rca` 共享模型 + 包注册 + 依赖（langgraph/celery/psycopg/redis）+ docker-compose。
2. `normalizer.py` + `test_rca_normalizer.py`。
3. `correlator.py` + `test_rca_correlator.py`。
4. fixtures（5 个 JSON/jsonl）。
5. `tools/` 5 工具 + ToolRegistry + `test_rca_tools.py`。
6. `store.py` PostgreSQL schema + CRUD + FakeRcaStore（测试）。
7. `runtime/` RCAState + nodes + graph + `test_rca_runtime.py`。
8. `tasks.py` Celery + `app.py` 10 端点 + `test_rca_api.py`。
9. 跑全量测试 + 端到端（docker-compose up + 真实 PG/Redis）。

---

**与现有 M1/M2 的关系**：
- 知识库工具复用 knowledge-api `/api/v1/chat/query`（M1/M2 已完成），不重复实现检索。
- common_schemas 复用（共享 Pydantic 模型模式）。
- PostgreSQL store 借鉴 M1 SQLiteStore 的参数化 SQL + 装饰器风格，但用 psycopg。
- LangGraph 0.x 是 M3 首次引入的新依赖（spec §4 技术栈明确）。
