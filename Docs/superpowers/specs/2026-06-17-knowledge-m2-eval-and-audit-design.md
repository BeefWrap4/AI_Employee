# Spec — Knowledge M2.1 评测 + 审计

日期：2026-06-17  
主题：M2 阶段二第一轮——离线评测（rag-eval）与知识库审计端点  
适用范围：`services/eval-service`、`services/knowledge-api`、`tests/rag-eval/`

## 1. 背景

M1 阶段已完成 RAG 闭环（`Docs/superpowers/specs/2026-06-17-knowledge-m1-persistence-ingestion-design.md`），但缺可度量与可审计能力：

- 无黄金问答集与离线评测脚本，检索质量无法回归比对。
- `qa_log` / `feedback` 表已建、写入已接通，但**无只读查询端点**，无法回溯「谁在什么时间查了什么、返回了哪些引用」。
- ACL 与检索降级 M1 留有缺口，统一推到 M2.2。

本 spec 把 M2 拆为两轮，本轮（M2.1）只做**评测 + 审计**。

## 2. 目标

- `services/eval-service`（纯 Python CLI 包）跑离线评测，输出 4 类指标 + JSON/Markdown 报表。
- `tests/rag-eval/golden.jsonl` 内置 12 条黄金问答，覆盖 hit / 越权 / 无知识三类。
- knowledge-api 新增 4 个只读端点：QA 日志列表/单条、反馈列表、文档列表。
- 全量 83 个 M1 测试保持通过；新增 ≥ 10 个 eval/audit 测试。

## 3. 非目标

- 用户/角色鉴权（SSO、RBAC）：M3/M5 统一处理，本轮无鉴权。
- 权限细化（chunk 级 ACL、引用二次校验）：推 M2.2。
- 检索降级（Qwen/FTS5 不可达的可解释降级）：推 M2.2。
- HTML / CSV 等其他报表格式：仅 JSON + Markdown。
- 大规模黄金集（>50 条）：MVP 12 条已覆盖三类场景。
- 远程评测 API（`POST /eval/runs` 异步）：仅 CLI。

## 4. 仓库与模块布局

```
AI_Employee/
├─ services/
│  ├─ eval-service/
│  │  └─ src/ai_employee/eval/
│  │     ├─ __init__.py
│  │     ├─ golden.py        # 黄金集加载与校验
│  │     ├─ runner.py        # 调 knowledge-api 跑评测、收集结果
│  │     ├─ metrics.py       # 指标计算
│  │     ├─ report.py        # JSON + Markdown 渲染
│  │     └─ __main__.py      # CLI 入口
│  └─ knowledge-api/
│     └─ src/ai_employee/knowledge_api/
│        ├─ store.py          # 修改：list_qa_logs / get_qa_log / list_feedbacks / list_documents
│        ├─ schemas.py        # 修改：QaLogResponse / QaLogListResponse / FeedbackListResponse
│        └─ app.py            # 修改：4 个审计 GET 端点
├─ tests/
│  ├─ rag-eval/
│  │  └─ golden.jsonl         # 12 条黄金问答
│  ├─ test_eval_golden.py
│  ├─ test_eval_metrics.py
│  ├─ test_eval_report.py
│  ├─ test_eval_runner.py
│  └─ test_audit_endpoints.py
└─ var/data/eval_reports/     # 报表输出（gitignore 已含 var/）
```

包注册：`pyproject.toml` 的 `[tool.setuptools]` 追加 `ai_employee.eval`，package-dir 映射到 `services/eval-service/src/ai_employee/eval`；`pytest.ini` pythonpath 追加 `services/eval-service/src`。

约束：
- eval-service 是纯 Python 包，无 FastAPI 依赖；只依赖 `httpx`（已在 deps）。
- 黄金集放 `tests/rag-eval/`，是测试夹具而非生产数据。
- 报表输出到 `var/data/eval_reports/`，`var/` 已 gitignore。

## 5. 黄金问答集

`tests/rag-eval/golden.jsonl`，JSON Lines 格式，每行一条。12 条覆盖三类：

| 场景 | 条数 | 标签 |
|---|---|---|
| 命中·无线（5G 接入） | 4 | `["hit","wireless"]` |
| 命中·传输 | 2 | `["hit","transport"]` |
| 越权拒答（transport 问题用 wireless scope） | 2 | `["refusal","out_of_scope"]` |
| 无知识拒答（完全无关） | 2 | `["refusal","no_knowledge"]` |
| 命中·边界（同义改写、模糊措辞） | 2 | `["hit","edge"]` |

每行字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `qid` | str | 问题唯一 ID，如 `q01` |
| `question` | str | 自然语言问题 |
| `expected_doc_title` | str \| null | 期望命中的文档**标题**；`null` 表示应拒答 |
| `scope` | list[str] | 查询时携带的 knowledge_scopes |
| `expect_refusal` | bool | `true`=期望 404；`false`=期望命中 |
| `tags` | list[str] | 分类标签，供报表分组统计 |

**关联键：title 而非 doc_id**。黄金集不依赖具体 doc_id 编号，文档重新入库也不失效。runner 启动时通过 `GET /api/v1/documents` 拉取已发布文档，建 `title→doc_id` 映射。

校验规则（`golden.load_golden`）：
- qid 唯一，缺失或重复 → 加载报错。
- `expect_refusal=true` 时 `expected_doc_title` 必须为 `null`；`false` 时必须非空。矛盾 → 报错。
- question 非空字符串。
- 文件不存在或空 → 报错。

## 6. eval-service CLI

```bash
python -m ai_employee.eval \
  --golden tests/rag-eval/golden.jsonl \
  --api http://127.0.0.1:8010 \
  --top-k 1,3,5 \
  --out var/data/eval_reports \
  --timeout 60
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--golden` | 必填 | 黄金集 JSONL 路径 |
| `--api` | `http://127.0.0.1:8000` | knowledge-api 基址 |
| `--top-k` | `1,3,5` | 评估的 K 值，逗号分隔 |
| `--out` | `var/data/eval_reports` | 报表输出目录 |
| `--timeout` | `60` | 单次查询超时秒 |
| `--threshold-top1` | `0.6` | Top-1 命中阈值 |
| `--threshold-top3` | `0.8` | Top-3 命中阈值 |
| `--threshold-refusal` | `0.9` | 拒答准确率阈值 |

退出码：0 = 全部阈值满足；1 = 任一指标低于阈值（便于 CI 卡门槛）。报表**始终生成**。

### runner 运行流程

```
1. load_golden(path) → list[GoldenItem]，校验失败立即退出 1
2. GET {api}/api/v1/documents 拿已发布文档列表 → title→doc_id 映射
3. 对每条 GoldenItem：
   a. 解析 expected_doc_title → expected_doc_id（hit 类）
   b. POST {api}/api/v1/chat/query {question, knowledge_scopes=scope}
      计时 latency_ms
   c. 记录 EvalResult：status_code、returned_doc_ids、answer、latency
4. metrics.compute(results, top_ks) → EvalMetrics
5. report.render(metrics) → 写 report_{ts}.json + report_{ts}.md 到 --out
6. stdout 打印摘要；按阈值判定退出码
```

## 7. 评测指标

### 输入：EvalResult

```python
@dataclass
class EvalResult:
    qid: str
    question: str
    expected_doc_id: str | None
    expect_refusal: bool
    status_code: int                # 200 或 404
    returned_doc_ids: list[str]     # citations 中的 doc_id，按召回顺序
    answer: str
    latency_ms: int
    error: str | None               # 网络异常等
```

### 输出：EvalMetrics

```python
@dataclass
class EvalMetrics:
    total: int
    errored: int
    refusal_violations: int
    hit_counts: dict[int, int]       # {1: 8, 3: 10, 5: 12}
    hit_rates: dict[int, float]      # {1: 0.67, 3: 0.83, 5: 1.0}
    eligible_for_hit: int
    citation_coverage: float
    refusal_expected: int
    refusal_correct: int
    refusal_accuracy: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_mean_ms: float
    per_item: list[dict]
```

### 判定逻辑

| 指标 | 判定 |
|---|---|
| Top-K 命中 | `expect_refusal=False` 且 `expected_doc_id in returned_doc_ids[:K]` |
| 引用覆盖 | `expect_refusal=False` 且 `status_code==200` 且 `returned_doc_ids` 非空且含 `expected_doc_id` |
| 拒答正确 | `expect_refusal=True` 且 `status_code==404` |
| 拒答误判 | `expect_refusal=True` 但 `status_code==200`（应拒未拒，最严重）→ `refusal_violations++` 并 per_item 标 `refusal_violation` |

### 边界

- HTTP 异常（network error）→ 该条 `errored++`，不计入命中/拒答分子，但计入 `total`。
- 延迟统计用纯 Python（排序取分位），不引入 numpy。

## 8. 报表渲染

输出到 `--out/report_{YYYYMMDD-HHMMSS}.{json,md}`。

### JSON 结构

```json
{
  "ts": "2026-06-17T15:30:00Z",
  "golden_path": "tests/rag-eval/golden.jsonl",
  "api_base": "http://127.0.0.1:8010",
  "top_ks": [1, 3, 5],
  "summary": {"total": 12, "errored": 0, "refusal_violations": 0},
  "metrics": {
    "hit_rates": {"1": 0.67, "3": 0.83, "5": 1.0},
    "hit_counts": {"1": 8, "3": 10, "5": 12},
    "eligible_for_hit": 12,
    "citation_coverage": 0.83,
    "refusal_expected": 4, "refusal_correct": 4, "refusal_accuracy": 1.0,
    "latency_p50_ms": 120.5, "latency_p95_ms": 340.2, "latency_mean_ms": 156.3
  },
  "thresholds": {"top1": 0.6, "top3": 0.8, "refusal": 0.9},
  "pass": true,
  "per_item": [
    {"qid": "q01", "verdict": "hit@1", "expected": "doc_001", "returned": ["doc_001"], "latency_ms": 110},
    {"qid": "q07", "verdict": "refusal", "expected": null, "status_code": 404, "latency_ms": 25}
  ]
}
```

### Markdown 表格

```markdown
# RAG 评测报告 — 2026-06-17 15:30:00
- API: http://127.0.0.1:8010
- 黄金集: tests/rag-eval/golden.jsonl（12 条）
- 结果: ✅ PASS

## 指标
| 指标 | 值 | 阈值 | 状态 |
|---|---|---|---|
| Total | 12 | — | — |
| Errored | 0 | — | — |
| 拒答误判 | 0 | — | — |
| Top-1 命中 | 67% (8/12) | ≥ 60% | ✅ |
| Top-3 命中 | 83% (10/12) | ≥ 80% | ✅ |
| 引用覆盖 | 83% (10/12) | — | ✅ |
| 拒答准确 | 100% (4/4) | ≥ 90% | ✅ |
| P50 延迟 | 121 ms | — | — |
| P95 延迟 | 340 ms | — | — |

## 明细
| qid | 判定 | expected | 返回 | latency |
|---|---|---|---|---|
| q01 | hit@1 | doc_001 | doc_001 | 110 ms |
| q07 | refusal | — | 404 | 25 ms |
```

`verdict` 取值：`hit@1` / `hit@3` / `hit@5` / `miss` / `refusal` / `refusal_violation` / `error`。状态 emoji：✅=满足阈值，❌=不满足。报表时间戳取 UTC。

## 9. knowledge-api 审计端点

### 端点列表

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/v1/qa-logs` | qa_log 列表（分页 + 过滤） |
| `GET` | `/api/v1/qa-logs/{trace_id}` | 单条 qa_log 详情（含 retrieved_chunks） |
| `GET` | `/api/v1/feedbacks` | feedback 列表（分页 + 过滤） |
| `GET` | `/api/v1/documents` | 已发布文档列表（评测需要，title→doc_id 映射来源） |

### 通用查询参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `session_id` | str | — | 精确匹配（qa-logs） |
| `user_id` | str | — | 精确匹配（qa-logs） |
| `trace_id` | str | — | 精确匹配（feedbacks） |
| `feedback_type` | enum | — | `useful`/`useless`/`wrong_citation`/`outdated` |
| `since` | ISO8601 | — | `created_at >= since` |
| `until` | ISO8601 | — | `created_at < until` |
| `page` | int | 1 | 1-based |
| `page_size` | int | 50 | 上限 200 |

### 响应模型

```python
class QaLogSummary(BaseModel):
    qa_log_id: str
    trace_id: str
    session_id: str
    user_id: str | None
    question: str
    answer: str
    confidence: float
    latency_ms: int
    model_name: str
    created_at: str

class QaLogResponse(QaLogSummary):
    retrieved_chunks: list[dict]

class QaLogListResponse(BaseModel):
    items: list[QaLogSummary]
    total: int
    page: int
    page_size: int

class FeedbackListResponse(BaseModel):
    items: list[FeedbackResponse]
    total: int
    page: int
    page_size: int
```

### store 新增方法

```python
def list_qa_logs(self, *, session_id=None, user_id=None, since=None, until=None,
                 page=1, page_size=50) -> tuple[list[dict], int]
def get_qa_log(self, trace_id: str) -> dict | None
def list_feedbacks(self, *, trace_id=None, feedback_type=None, since=None, until=None,
                   page=1, page_size=50) -> tuple[list[dict], int]
def list_documents(self, *, status=None, page=1, page_size=50) -> tuple[list[dict], int]
```

全部使用 sqlite3 参数化查询避免注入。`total` 用 `COUNT(*)` 单独计算。

### 鉴权

本轮无鉴权（本地单用户信任）。spec 明确标注"本地信任"，M3/M5 统一接入 SSO/RBAC。

## 10. 测试策略

### 单元测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_eval_golden.py` | 黄金集加载：合法 12 条；qid 重复报错；refusal vs title 互斥；空文件报错 |
| `tests/test_eval_metrics.py` | 指标计算：mock EvalResult；Top-1/3/5 hit_rates；refusal_accuracy；HTTP 异常不计分子；分位延迟 |
| `tests/test_eval_report.py` | 报表渲染：JSON / Markdown；verdict 字段（hit@1, miss, refusal, refusal_violation, error）；阈值通过/失败状态 |

### 集成测试（in-process TestClient）

| 文件 | 覆盖 |
|---|---|
| `tests/test_eval_runner.py` | runner 端到端：上传 2 篇 markdown + publish → 跑 4 条黄金（2 hit + 1 越权 + 1 无知识）→ 报表字段正确；HTTP 错误注入（api 不可达）→ errored 计数 |
| `tests/test_audit_endpoints.py` | 审计端点：先 query 一次 → 列表 + 单条 trace_id 命中 → 过滤 session_id/since 生效 → 反馈端点；分页边界 |

### M1 回归

- 跑全量 83 测试，确保新增代码不破坏现有行为。
- 特别关注：store 新增方法不改变 `write_qa_log` / `write_feedback` 既有调用。

### 关键 fixture（`tests/conftest.py` 扩展）

```python
@pytest.fixture
def eval_workspace(api_factory, tmp_path):
    """准备 2 篇已发布文档 + 黄金子集。"""
    client = api_factory()
    doc1 = _upload_and_publish(client, title="5G 接入排障 SOP", content="...", metadata={"network_type":"5g"}, acl_tags=["wireless"])
    doc2 = _upload_and_publish(client, title="传输链路 SOP", content="...", metadata={"network_type":"transport"}, acl_tags=["transport"])
    golden = tmp_path / "golden.jsonl"
    golden.write_text(make_golden_lines(doc1, doc2))
    return client, golden
```

### 不在 M2.1 测试范围

- 并发评测（CLI 单线程足够 12 条）。
- 大规模数据集 / 压测。
- 鉴权 / RBAC 测试。

## 11. 验收

- `python -m pytest` 全部通过（83 M1 + ≥ 10 M2.1）。
- `python -m ai_employee.eval --golden tests/rag-eval/golden.jsonl --api http://127.0.0.1:8010` 跑通：12 条全跑、无 errored、Top-3 ≥ 80%、拒答准确 ≥ 90%、退出码 0。
- 报表 JSON 与 Markdown 落盘到 `var/data/eval_reports/`，含 ts、summary、metrics、thresholds、pass、per_item。
- knowledge-api `GET /api/v1/qa-logs?session_id=xxx` 返回过滤后列表，分页字段正确。
- knowledge-api `GET /api/v1/qa-logs/{trace_id}` 命中单条，含 `retrieved_chunks` 完整列表。
- knowledge-api `GET /api/v1/documents` 列出已发布文档，给出 title / doc_id / parse_status / chunk_count。
- 端到端验证：先 query 一次 → 通过审计端点回溯到具体 trace_id → 看到 question / answer / citations / latency / created_at。

## 12. 实施拆分（建议执行顺序）

1. 黄金集 `tests/rag-eval/golden.jsonl` 12 条（先固化数据）。
2. eval-service `golden.py` + `test_eval_golden.py`。
3. eval-service `metrics.py` + `test_eval_metrics.py`。
4. eval-service `report.py` + `test_eval_report.py`。
5. knowledge-api store 新增 4 个 list/get 方法 + `test_store_sqlite.py` 补充用例。
6. knowledge-api 4 个审计端点 + `test_audit_endpoints.py`。
7. eval-service `runner.py` + `__main__.py` + `test_eval_runner.py`。
8. README 与 `.env.example` 补充 `EVAL_REPORTS_DIR`（可选）。
9. 跑全量测试 + 真实端到端评测。

---

**与 M1 的关系**：M1 spec 已在 `qa_log` / `feedback` 表上预埋全部字段（`Docs/superpowers/specs/2026-06-17-knowledge-m1-persistence-ingestion-design.md` §5.1），本 spec 不变更表结构，只补只读访问。
