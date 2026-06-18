# Spec — M6 平台闭环：知识回流 + 评测中心

日期：2026-06-17  
主题：M6 里程碑——RCA 报告知识回流（候选知识审核流）+ 平台评测中心（RAG/RCA 统一历史）  
适用范围：`services/rca-agent`、`services/agent-platform-api`、`packages/common-schemas`

参考：`Docs/ai-agent-telecom-projects-implementation-plan.md` §5.1（知识回流、评测中心）、§6（M6 验收）

## 1. 背景

M3/M4/M5 已完成（RCA 原型/验收 + 平台核心：Runtime/工具注册/审批/trace/三类模板）。M6 闭环两件未做：

- **知识回流**（spec §5.1）：确认后的 RCA 报告进入候选知识池，未审核不进正式知识库。当前完全缺失。
- **评测中心**（spec §5.1）：历史 run 回放、标准任务集、版本对比，复用 RAG 评测和 RCA 回放。当前 eval-service（RAG）与 rca replay（RCA）各自独立，未统一为平台能力。

M6 验收（实施计划 §6）：**确认后的报告可进入候选知识审核流**。

## 2. 目标

- rca-agent SQLite 新增 `candidate_knowledge` 表；RCA 报告 accepted 后按假设拆分为 N 条候选，进入 pending 审核流。
- 候选审核 approved 后调 knowledge-api `/api/v1/documents` 导入为 text/markdown 文档；不自动 publish（专家在 knowledge-api 侧审核 publish）。
- agent-platform-api 新增 `eval_runs` 表 + 3 个评测端点，统一 RAG/RCA 评测为平台能力，带历史。
- 全量 193 现有测试保持通过；新增 ≥ 10 个 M6 测试。

## 3. 非目标

- 候选知识自动 publish（永久非目标，spec §5.1"未审核内容不进入正式知识库"）。
- 评测中心图表/趋势可视化（M6 只提供数据 + 历史查询）。
- 评测异步执行（M6 规模小，同步；M7+ 再异步化）。
- 候选知识去重/相似度合并（M7+）。
- Web 门户候选审核页面（M7+）。

## 4. 仓库与模块布局

```
AI_Employee/
├─ services/rca-agent/src/ai_employee/rca_agent/
│  ├─ store.py                 # 修改：candidate_knowledge 表 + CRUD
│  ├─ schemas.py               # 修改：CandidateKnowledge 响应模型
│  ├─ app.py                   # 修改：候选知识端点
│  └─ knowledge_feedback.py    # 新增：报告→候选拆分 + approved→导入
├─ services/agent-platform-api/src/ai_employee/agent_platform_api/
│  ├─ eval_store.py            # 新增：eval_runs SQLite 持久化
│  ├─ schemas.py               # 修改：EvalRun 请求/响应模型
│  └─ app.py                   # 修改：/evaluations/runs 端点
├─ packages/common-schemas/src/ai_employee/common_schemas/
│  └─ eval.py                  # 新增：UnifiedReport 共享模型
├─ tests/
│  ├─ test_knowledge_feedback.py
│  ├─ test_knowledge_feedback_import.py
│  └─ test_eval_center.py
```

约束：
- 知识回流在 rca-agent 侧闭环；approved 后跨服务 HTTP 调 knowledge-api 导入。
- 评测中心 eval_runs 用独立 SQLite（`platform_eval.sqlite3`），不破坏平台现有内存 run 逻辑。
- 复用 eval-service（RAG）与 rca replay（RCA）作为评测执行器，平台只做编排 + 历史存储。
- 无新依赖（httpx/SQLite 已有）。

## 5. 候选知识表与回流拆分

### 5.1 candidate_knowledge 表（rca-agent SQLite）

| 字段 | 类型 | 说明 |
|---|---|---|
| `candidate_id` | TEXT PK | `ck_001` |
| `source_report_id` | TEXT NOT NULL | 来源 RCA 报告 |
| `source_incident_id` | TEXT NOT NULL | 来源 incident |
| `hypothesis_id` | TEXT NOT NULL | 拆分自哪个假设 |
| `root_cause_type` | TEXT NOT NULL | 根因类型 |
| `title` | TEXT NOT NULL | 候选标题（假设描述截断 80 字） |
| `content` | TEXT NOT NULL | 候选正文（假设描述 + final_root_cause + 处置建议） |
| `evidence_summary` | TEXT NOT NULL | 证据条目摘要（source_type + content 列表） |
| `review_status` | TEXT NOT NULL DEFAULT 'pending' | pending/approved/rejected |
| `reviewer` | TEXT | |
| `review_comment` | TEXT | |
| `imported_doc_id` | TEXT NULL | approved 导入后的 knowledge-api doc_id |
| `created_at` | TEXT NOT NULL | |
| `reviewed_at` | TEXT | |

### 5.2 回流拆分逻辑（knowledge_feedback.py）

`generate_candidates_from_report(report, incident, evidence) -> list[CandidateKnowledge]`：
- 仅当 report.review_status == "accepted" 且 final_root_cause 非空时触发。
- 遍历 report.hypotheses，每个 hypothesis 生成一条候选：
  - `title` = hypothesis.description（截断 80 字）
  - `content` = 假设描述 + final_root_cause + 处置建议
  - `evidence_summary` = 该假设 supporting_evidence_ids 对应的 evidence（source_type + content）拼接
  - `root_cause_type` = hypothesis.root_cause_type
- 入库 review_status=pending。

### 5.3 触发时机

`POST /api/v1/rca/reports/{report_id}/review` 处理 `decision=accepted` 时，**自动**调 `generate_candidates_from_report` 生成候选。一次 accepted 生成 N 条候选（N = 报告假设数）。

## 6. 候选知识审核流与导入

### 6.1 端点（rca-agent）

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/v1/candidate-knowledge` | 候选列表（过滤 review_status/incident_id + 分页） |
| `GET` | `/api/v1/candidate-knowledge/{candidate_id}` | 单条候选详情 |
| `POST` | `/api/v1/candidate-knowledge/{candidate_id}/review` | 审核（approved/rejected） |
| `POST` | `/api/v1/candidate-knowledge/{candidate_id}/import` | approved 候选导入 knowledge-api |

### 6.2 审核端点

`POST /candidate-knowledge/{candidate_id}/review`：
```json
{"decision": "approved", "reviewer": "expert_01", "comment": "确认根因正确"}
```
- `decision=approved` → review_status=approved + reviewed_at + reviewer + comment。
- `decision=rejected` → review_status=rejected + 同上，不导入。
- 已 reviewed 的候选再审 → 409 `already_reviewed`。

### 6.3 导入端点

`POST /candidate-knowledge/{candidate_id}/import`：
- 前置：review_status == approved，否则 409 `not_approved`。
- 已导入（imported_doc_id 非空）→ 409 `already_imported`。
- 调 knowledge-api `POST /api/v1/documents`（multipart）：
  - `file` = content 作为 `{title}.md`
  - `title` = candidate.title
  - `metadata_json` = `{"source": "rca_feedback", "incident_id": ..., "root_cause_type": ...}`
  - `acl_tags_json` = `["rca_feedback"]`
  - `version` = `v1`，`mime_type` = `text/markdown`
- 导入成功 → 更新 `imported_doc_id`。
- knowledge-api 不可达 → 503 `knowledge_api_unavailable`，候选保持 approved 未导入，可重试。

### 6.4 导入后状态

- imported_doc_id 记录 knowledge-api doc_id。
- 候选不可重复导入（already_imported 保护）。
- knowledge-api 侧文档走既有流程（uploaded → worker 解析 → ready → 需人工 publish）。**M6 不自动 publish**。

## 7. 评测中心（带历史）

### 7.1 eval_runs 表（agent-platform-api SQLite）

独立文件 `${RCA_DATA_DIR}/platform_eval.sqlite3`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `eval_run_id` | TEXT PK | `eval_001` |
| `eval_type` | TEXT NOT NULL | rag / rca |
| `template_id` | TEXT NOT NULL | knowledge_query / rca / inspection |
| `golden_path` | TEXT NOT NULL | 黄金集路径 |
| `status` | TEXT NOT NULL | pending/running/completed/failed |
| `report_json` | TEXT NULL | 统一报表 JSON |
| `summary` | TEXT NULL | 摘要（total/top1/top3/evidence_coverage） |
| `error` | TEXT NULL | |
| `trace_id` | TEXT NOT NULL | |
| `created_at` | TEXT NOT NULL | |
| `completed_at` | TEXT NULL | |

### 7.2 端点（agent-platform-api）

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/evaluations/runs` | 发起评测（同步执行，存历史） |
| `GET` | `/api/v1/evaluations/runs` | 历史列表（过滤 eval_type/status + 分页） |
| `GET` | `/api/v1/evaluations/runs/{eval_run_id}` | 单次评测报表详情 |

### 7.3 POST /evaluations/runs

```json
{"eval_type": "rag", "template_id": "knowledge_query", "golden_path": "tests/rag-eval/golden.jsonl", "api_base": "http://127.0.0.1:8010"}
```
- `eval_type=rag` → 调 eval-service `runner.run(golden_path, api_base)` + `metrics.compute` + `report.build_report`，转统一报表。
- `eval_type=rca` → 调 rca-agent replay（`replay.run_replay(golden_path)`），转统一报表格式。
- 同步执行（M6 规模小，无需 Celery）。
- 存 eval_runs 记录（status=completed + report_json + summary）。
- 返回 eval_run_id + 报表。

### 7.4 统一报表格式（common_schemas.eval.UnifiedReport）

```python
@dataclass
class UnifiedReport:
    eval_type: str           # rag/rca
    total: int
    top1_coverage: float     # RAG: Top-1 命中率；RCA: Top-1 根因覆盖率
    top3_coverage: float
    evidence_coverage: float # RAG: 引用覆盖；RCA: 证据覆盖
    refusal_accuracy: float | None  # RAG 专有；RCA 为 None
    latency_p95_ms: float | None    # RAG 专有；RCA 为 None
    per_item: list[dict]
    raw_report: dict         # 原始 RAG/RCA 报表
```

### 7.5 历史与版本对比

- `GET /evaluations/runs?eval_type=rag&template_id=knowledge_query` → 按类型/模板过滤历史。
- 客户端拉取多次 eval_run 对比 top1/top3 趋势（M6 只提供数据，不做图表）。

## 8. 测试策略

### 8.1 单元测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_knowledge_feedback.py` | 候选拆分：accepted → N 条候选（N=假设数）；rejected 不生成；evidence_summary 关联正确；列表过滤 + 分页；审核 approved/rejected 状态转换；已审核 409 |
| `tests/test_knowledge_feedback_import.py` | 导入：approved → 调 knowledge-api multipart（mock httpx）→ imported_doc_id 更新；未 approved 409；已导入 409；knowledge-api 不可达 503 + 候选保持 approved |
| `tests/test_eval_center.py` | 评测中心：POST rag → 调 eval-service（mock）→ 统一报表 + 存历史；POST rca → 调 replay（mock）→ 统一报表；历史列表过滤；单次查询 404 |

### 8.2 集成测试（in-process）

- `test_knowledge_feedback.py`：上传告警 → build incident → create run → review accepted → 自动生成候选 → 审核 approved → import（mock knowledge-api）→ imported_doc_id 非空。
- `test_eval_center.py`：mock eval-service runner 与 rca replay，验证统一报表字段 + 历史持久化。

### 8.3 端到端（可选，标注 e2e）

- 真实 knowledge-api + rca-agent + agent-platform：RCA accepted → 候选 → approved → import → knowledge-api 出现新文档。`@pytest.mark.e2e` 默认跳过。

### 8.4 回归

- 全量 193 现有 + ≥ 10 新增 = ≥ 203 测试通过。
- 不破坏现有 RCA review 流程（accepted 仍记录 review，额外触发候选拆分）。

## 9. 验收

- `python -m pytest` 全部通过（193 + ≥ 10）。
- 端到端：RCA 报告 accepted → 自动生成 N 条候选知识（pending）→ 专家 approved → import → knowledge-api 出现新文档（uploaded → ready）→ imported_doc_id 记录。
- rejected 候选不可导入（409）。
- 评测中心：POST rag 评测 → 统一报表（top1/top3/evidence_coverage）+ 存历史；GET 历史列表过滤；GET 单次报表。
- 评测中心：POST rca 评测 → 统一报表（top1/top3 根因覆盖）。
- knowledge-api 不可达时 import 返回 503，候选保持可重试。

## 10. 实施拆分（建议执行顺序）

1. `common_schemas.eval.UnifiedReport` 共享模型。
2. rca-agent `candidate_knowledge` 表 + store CRUD + `knowledge_feedback.py` 拆分逻辑 + `test_knowledge_feedback.py`。
3. rca-agent 候选知识 4 端点（list/get/review/import）+ `test_knowledge_feedback_import.py`。
4. RCA review accepted 触发自动候选拆分（改 app.py review 端点）。
5. agent-platform-api `eval_store.py` eval_runs 表 + `test_eval_center.py`。
6. agent-platform-api 3 个评测端点 + RAG/RCA 适配器 + `test_eval_center.py`。
7. 跑全量测试 + 端到端（真实三服务）。

---

**与现有 M3/M4/M5 的关系**：
- 知识回流复用 M4 的 RCA review 流程（accepted 状态），在其上叠加候选拆分。
- 评测中心复用 M2.1 的 eval-service（RAG）与 M4 的 rca replay（RCA），平台层做统一编排 + 历史。
- 导入复用 M1 的 knowledge-api `/api/v1/documents` multipart 上传。
- M6 闭环后，三项目（RAG 知识库 / RCA Agent / 运维平台）形成"知识→诊断→回流→知识"循环。
