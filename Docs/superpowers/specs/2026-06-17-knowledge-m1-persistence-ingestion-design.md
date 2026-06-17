# Spec — Knowledge M1 收尾：SQLite 持久化 + ingestion-worker

日期：2026-06-17  
主题：M1 阶段一收尾 — 真实持久化与独立 ingestion 微服务  
适用范围：`services/knowledge-api` 与 `services/ingestion-worker`

## 1. 背景

`Docs/ai-agent-telecom-projects-implementation-plan.md` 中 M1「RAG 闭环」要求把文档从入库到带引用问答打通。当前 `services/knowledge-api` 已实现：

- 文档创建 / 查询 / 发布 / 段落分块 / 引用问答 / 反馈（commit `21a9946` 等）
- 内存版 `InMemoryKnowledgeStore`，无持久化，无独立解析服务

缺口：

- 进程重启即丢数据，QA 历史无法回溯
- 没有真实的解析器（仅按空行切段）
- 没有向量与 FTS5 倒排
- 没有任何异步任务支撑
- `services/ingestion-worker`、`packages/common-schemas` 仍是空目录

本 spec 把 M1 收尾为"可演示 MVP"：用 SQLite 做持久化、新增独立 ingestion-worker、引入 EmbeddingProvider 与 FTS5、把现有内存实现平滑迁出。

## 2. 目标

- `knowledge-api` 改用 SQLite，重写现有 M1 测试以验证行为不变。
- 新增 `services/ingestion-worker` 微服务，通过 HTTP 提供解析与 embedding 能力。
- 文档状态机扩展为 6 态：uploaded / parsing / parse_failed / ready / published / archived。
- 检索走 FTS5 + 向量余弦 + 范围过滤的三步召回，替换现有 token 重合度打分。
- 端到端流程：multipart 上传 → 落盘 → 派发 worker → 解析回写 → 发布 → 问答命中。

## 3. 非目标

- 真实 PDF / Word / Excel 解析（仅占位实现，返回明确 `parse_error="mime_unsupported"`）。
- 真实 BGE-M3 / OpenAI 模型推理（仅 stub + OpenAI-compatible 抽象，可远程调真实接口）。
- 评测、ACL 复杂策略、降级（保留为 M2 范围，仅在 spec 中预留接口）。
- K8s/Helm 部署（保持本地双进程即可）。
- Web 门户、`apps/web-portal`（推迟）。
- RCA Agent 任何相关服务。

## 4. 仓库与模块布局

新增 / 重写文件如下：

```
AI_Employee/
├─ services/
│  ├─ knowledge-api/
│  │  └─ src/ai_employee/knowledge_api/
│  │     ├─ app.py            # 重写：替换 InMemoryKnowledgeStore
│  │     ├─ store.py          # 新增：SQLiteStore + 初始化 schema
│  │     ├─ schemas.py        # 重构：抽出 Pydantic 模型
│  │     ├─ retrieval.py      # 新增：FTS5 + 向量召回 + 范围过滤
│  │     └─ worker_client.py  # 新增：调用 ingestion-worker
│  ├─ ingestion-worker/
│  │  └─ src/ai_employee/ingestion_worker/
│  │     ├─ app.py            # 新增：FastAPI 入口（/internal/parse, /health）
│  │     ├─ parsers.py        # 新增：markdown/html/text 解析器
│  │     ├─ chunker.py        # 新增：段落切分 + heading 路径
│  │     └─ embedding.py      # 新增：EmbeddingProvider + Stub + OpenAICompat
├─ packages/
│  └─ common-schemas/
│     └─ src/ai_employee/common_schemas/
│        ├─ __init__.py
│        └─ knowledge.py      # 新增：Document/Chunk/Citation 共享 schema
├─ tests/
│  ├─ test_knowledge_api_m1.py   # 重写：改用 SQLite fixture
│  ├─ test_ingestion_worker_m1.py # 新增
│  ├─ test_chunker.py            # 新增
│  ├─ test_parsers.py            # 新增
│  ├─ test_embedding_stub.py     # 新增
│  ├─ test_state_machine.py      # 新增
│  ├─ test_acl_filter.py         # 新增
│  ├─ test_fts5_recall.py        # 新增
│  ├─ test_internal_auth.py      # 新增
│  └─ test_parse_failure_flow.py # 新增
├─ var/data/raw/             # 新增：原始文件落盘目录（gitignore）
└─ .env.example               # 新增：列出可选的 Embedding 配置
```

`pyproject.toml` 中 `[tool.setuptools.packages.find].where` 追加：

- `services/knowledge-api/src`
- `services/ingestion-worker/src`
- `packages/common-schemas/src`

`.gitignore` 追加：

- `var/`
- `.env`

## 5. 数据模型

### 5.1 SQLite 表

**`documents`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `doc_id` | TEXT PRIMARY KEY | `doc_001` 形式 |
| `title` | TEXT NOT NULL | 文档标题 |
| `source_uri` | TEXT NOT NULL | 原始文件绝对路径 |
| `mime_type` | TEXT NOT NULL | 上传时声明的 mime |
| `metadata_json` | TEXT NOT NULL | JSON 字符串 |
| `acl_tags_json` | TEXT NOT NULL | JSON 字符串列表 |
| `parse_status` | TEXT NOT NULL | CHECK 约束：6 个状态值 |
| `parse_error` | TEXT NULL | 解析失败原因 |
| `chunk_count` | INTEGER NOT NULL DEFAULT 0 | 缓存字段 |
| `version` | TEXT NOT NULL DEFAULT 'v1' | 用户传入的版本号 |
| `created_at` | TEXT NOT NULL | ISO8601 |
| `updated_at` | TEXT NOT NULL | ISO8601 |

**`chunks`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `chunk_id` | TEXT PRIMARY KEY | `chunk_{doc_id}_{seq:03d}` |
| `doc_id` | TEXT NOT NULL | FK → `documents.doc_id`，ON DELETE CASCADE |
| `chunk_no` | INTEGER NOT NULL | 段内序号 |
| `content` | TEXT NOT NULL | 段文本 |
| `section_path` | TEXT NOT NULL | 标题路径 |
| `page_no` | INTEGER NOT NULL DEFAULT 1 | MVP 固定 1 |
| `embedding_json` | TEXT NULL | JSON 数组（与 chunk 1:1） |
| `embedding_model` | TEXT NULL | 生成该向量的模型名 |
| `created_at` | TEXT NOT NULL | ISO8601 |

**`chunks_fts`**（FTS5 虚拟表，外部内容表）

```sql
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  chunk_id UNINDEXED,
  content,
  section_path,
  tokenize='unicode61'
);
```

触发器：`chunks` 增删改时同步维护 `chunks_fts`。

**`qa_logs`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `qa_log_id` | TEXT PRIMARY KEY | `qa_001` 形式 |
| `session_id` | TEXT NOT NULL | 客户端会话 ID |
| `user_id` | TEXT NULL | 预留（M2 接入 SSO） |
| `question` | TEXT NOT NULL | 原始问题 |
| `rewritten_query` | TEXT NULL | 改写后（M2 引入） |
| `retrieved_chunks_json` | TEXT NOT NULL | 命中片段 JSON |
| `answer` | TEXT NOT NULL | 模型答案 |
| `model_name` | TEXT NOT NULL | 模型标识 |
| `prompt_version` | TEXT NOT NULL | 提示词版本 |
| `confidence` | REAL NOT NULL | 置信度 |
| `latency_ms` | INTEGER NOT NULL | 端到端耗时 |
| `trace_id` | TEXT NOT NULL UNIQUE | 链路追踪 ID |
| `created_at` | TEXT NOT NULL | ISO8601 |

**`feedbacks`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `feedback_id` | TEXT PRIMARY KEY | `fb_001` 形式 |
| `qa_log_id` | TEXT NULL | 关联问答日志 |
| `trace_id` | TEXT NOT NULL | 冗余便于按 trace 检索 |
| `feedback_type` | TEXT NOT NULL | `useful` / `useless` / `wrong_citation` / `outdated` |
| `comment` | TEXT NULL | 备注 |
| `user_id` | TEXT NULL | 预留 |
| `created_at` | TEXT NOT NULL | ISO8601 |

### 5.2 文件落盘

- 原始文件：`${KNOWLEDGE_DATA_DIR}/raw/{doc_id}.{ext}`，ext 由 mime_type 决定（`md` / `html` / `txt`）。
- 写文件用 `tempfile.NamedTemporaryFile + os.replace`，确保不会读到半写文件。
- `KNOWLEDGE_DATA_DIR` 启动时校验可写；不存在则自动创建。

### 5.3 Embedding 存储格式

- `chunks.embedding_json` 存 JSON 数组（如 `[0.12, -0.04, ...]`），本地 MVP 数据量下不构成瓶颈。
- 后续可加 `embedding_dim` 字段约束或迁出到独立向量库；本 spec 不引入。

## 6. API 表面

### 6.1 knowledge-api 公开端点

| 方法 | 路径 | 变化 | 说明 |
|---|---|---|---|
| `POST` | `/api/v1/documents` | **改 multipart** | 字段：`file`（必填）、`title`、`metadata_json`、`acl_tags_json`、`version`、`mime_type`（可声明，服务端校验）。返回 202 + `doc_id`、`parse_status=uploaded`、`trace_id`、`worker_dispatch`。 |
| `GET` | `/api/v1/documents/{doc_id}` | 响应扩展 | 增加 `parse_error`、`updated_at`、`mime_type`、`version`。 |
| `GET` | `/api/v1/documents/{doc_id}/chunks` | 不变 | 数据从 SQLite 读。 |
| `POST` | `/api/v1/documents/{doc_id}/publish` | **状态前置** | 仅 `ready` 允许；否则 409。 |
| `POST` | `/api/v1/documents/{doc_id}/reparse` | 新增 | `parse_failed → uploaded`，重新派发 worker。 |
| `POST` | `/api/v1/documents/{doc_id}/archive` | 新增 | `published → archived`。 |
| `POST` | `/api/v1/documents/{doc_id}/restore` | 新增 | `archived → published`。 |
| `POST` | `/api/v1/chat/query` | 检索逻辑 | 改走 `retrieval.search()`，不再用 token 重合度。 |
| `POST` | `/api/v1/feedback` | 不变 | 写 `feedbacks` 表。 |
| `GET` | `/health` | 扩展字段 | `storage=sqlite`、`ingestion_worker_reachable=true/false`、`embedding_provider_degraded=true/false`。 |

### 6.2 knowledge-api 内部端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `POST` | `/internal/chunks` | `KNOWLEDGE_API_INTERNAL_TOKEN` | 接收 worker 回写，事务化写 `chunks` + FTS5 + 更新 `documents.parse_status=ready` |
| `POST` | `/internal/documents/{doc_id}/parse-failed` | 同上 | 接收 worker 失败回写，状态机 `→ parse_failed` |

内部端点不写入 OpenAPI 文档，不在 README 公开。

### 6.3 ingestion-worker 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/internal/parse` | 输入 `{doc_id, file_path, mime_type, metadata}`；输出 `{chunks, embeddings, embedding_model}` |
| `GET` | `/health` | 报告 worker 状态 + `embedding_provider=stub/openai` + `last_call_ok` |

### 6.4 配置项

| 变量 | 默认 | 用途 |
|---|---|---|
| `KNOWLEDGE_DATA_DIR` | `<repo>/var/data` | 原始文件 + SQLite 目录 |
| `KNOWLEDGE_SQLITE_PATH` | `${DATA_DIR}/knowledge.sqlite3` | SQLite 文件路径 |
| `INGESTION_WORKER_URL` | `http://127.0.0.1:8001` | knowledge-api 调 worker 地址 |
| `INGESTION_WORKER_TIMEOUT_S` | `30` | 单次解析超时 |
| `KNOWLEDGE_API_INTERNAL_TOKEN` | （必填） | worker ↔ api 共享 token |
| `EMBEDDING_PROVIDER` | `stub` | `stub` / `openai_compat` |
| `EMBEDDING_BASE_URL` | 空 | OpenAI-compatible 基础 URL |
| `EMBEDDING_MODEL` | 空 | 模型名 |
| `EMBEDDING_API_KEY` | 空 | 鉴权 token |
| `EMBEDDING_DIM` | `8`（stub）/ provider 决定 | 向量维度 |
| `MAX_UPLOAD_BYTES` | `10485760`（10MB） | 上传硬限制 |

`.env.example` 列出所有变量并标注必填 / 默认值。

## 7. 文档状态机

### 7.1 状态集合

`uploaded`、`parsing`、`parse_failed`、`ready`、`published`、`archived`。

### 7.2 合法转换

```
uploaded ──worker 接受──▶ parsing ──worker 完成回写──▶ ready
   ▲                        │
   │                        └─ worker 失败回写 ──▶ parse_failed ──/reparse──▶ uploaded
   │
published ──/archive──▶ archived ──/restore──▶ published
   ▲                                                  
   └─── /publish（仅 ready 可达）──────────────────────┘
```

### 7.3 约束

- 数据库 `CHECK` 约束 6 个状态值。
- 应用层白名单 `allowed_transitions: dict[ParseStatus, set[ParseStatus]]`。
- `published` 文档不允许 `/reparse`，需新建版本。
- `parse_failed` 文档保留磁盘文件，便于重试时复用。

## 8. 切分与 Embedding 流水线

### 8.1 解析器（按 mime_type 路由）

| mime_type | 解析器 | 行为 |
|---|---|---|
| `text/markdown` | `MarkdownParser` | 用 `markdown-it-py` 转 AST；按 `#/##/###` 节点构造 `section_path` |
| `text/html` | `HtmlParser` | 用 `beautifulsoup4` 解析；按 `h1/h2/h3` 切片 |
| `text/plain` | `TextParser` | 按空行分段，`section_path="root"` |
| `application/pdf` / 其他 | `NotImplementedParser` | 返回 `parse_error="mime_unsupported: ..."` |

所有解析器返回 `ParsedSection { section_path: str, blocks: list[str] }`。

### 8.2 Chunker 策略

1. 段落级：每个 block 视作候选 chunk。
2. 窗口合并：相邻 block 合并后 `< 200 字` 且 `< 3 段` → 合并为一条 chunk。
3. 超长截断：单 block `> 800 字` → 按句号/换行硬切。
4. 告警码保护：匹配 `^[A-Z]{2,}-\d{2,}` 的 token 不被切分符切断。

每条 chunk：`{chunk_id, chunk_no, content, section_path, page_no=1}`。

### 8.3 EmbeddingProvider 抽象

```python
class EmbeddingProvider(Protocol):
    name: str
    dim: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class StubEmbeddingProvider:
    """基于 token hash 映射到 [-1, 1] 的伪向量，固定 dim=8，零依赖。"""

class OpenAICompatEmbeddingProvider:
    """POST {base_url}/v1/embeddings，启动时探测 dim。"""
```

启动时若 `EMBEDDING_PROVIDER=openai_compat` 但 base_url/api_key 缺失 → 自动降级 stub，标注 `embedding_provider_degraded=true`。

### 8.4 检索（knowledge-api）

`retrieval.search(question, scopes)` 三步走：

1. **范围过滤**：`SELECT doc_id FROM documents WHERE parse_status='published' AND acl_ok(...)`。
2. **FTS5 召回**：`SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? AND chunk_id IN (范围)`，Top-20。
3. **向量召回**：对范围内 chunk 算余弦相似度，Top-20。
4. **合并重排**：`0.5 * bm25_norm + 0.5 * vec_norm`，取 Top-K（默认 3）。
5. **拒答**：FTS5 与向量均无结果 → 404 `no_knowledge_in_scope`。

## 9. 错误处理

### 9.1 失败分类

| 失败点 | 错误码 | 系统行为 |
|---|---|---|
| 磁盘写失败 | `storage_write_failed` | 事务回滚，不创建 `documents` 记录 |
| SQLite 写失败 | `db_write_failed` | 临时文件清理，不创建 `documents` 记录 |
| mime 不支持 | `mime_unsupported` | 415 |
| 请求体超大 | `payload_too_large` | 413 |
| Worker 调用超时 | 文档保持 `uploaded` | 202 + `worker_dispatch=timeout` |
| Worker 解析失败 | 文档 → `parse_failed` | 保留磁盘文件 |
| Embedding 失败 | 文档 → `parse_failed` | `parse_error="embed_unavailable"` |
| Worker 内部 5xx | 文档保持 `uploaded` | 502 `worker_error` |
| 内部 token 校验失败 | `internal_unauthorized` | 401 |
| 范围内无 published 文档 | `no_knowledge_in_scope` | 404 |
| FTS5 损坏 | `index_corrupted` | 500，启动时检测并退出 |
| SQLite 锁 | `db_locked` | 500，3 次指数退避后退出 |

### 9.2 重试与超时

- knowledge-api → worker：30s 超时，网络错误重试 1 次。
- worker → EmbeddingProvider：10s 单次超时，连续 3 次失败 → `parse_failed`。
- worker → knowledge-api 回写：5s 单次超时，失败由后台 reconcile 任务扫描 `parsing > 5min` 文档重试（M2 完善，本期仅在文档说明）。

### 9.3 错误响应格式

```json
{
  "error_code": "parse_failed",
  "message": "embedding 不可用：openai_compat API 返回 401",
  "trace_id": "trace_doc_001_parse",
  "details": { "doc_id": "doc_001", "stage": "embed" }
}
```

### 9.4 安全边界

- `/internal/*` 仅接受 `KNOWLEDGE_API_INTERNAL_TOKEN`，不在 OpenAPI 暴露。
- `source_uri` 路径校验：仅接受 `${KNOWLEDGE_DATA_DIR}/raw/` 下的绝对路径。
- 上传文件大小硬限制 10MB（FastAPI 中间件）。

## 10. 测试策略

### 10.1 单元测试

| 文件 | 覆盖点 |
|---|---|
| `tests/test_chunker.py` | 段落切分、窗口合并、超长截断、告警码保护 |
| `tests/test_parsers.py` | Markdown/HTML/text 解析器对样例文件输出 |
| `tests/test_embedding_stub.py` | StubEmbeddingProvider 确定性、dim 校验 |
| `tests/test_state_machine.py` | 6 态合法/非法转换、parse_error 写入 |
| `tests/test_acl_filter.py` | scope 过滤对 acl_tags + metadata 命中 |
| `tests/test_fts5_recall.py` | BM25 排序、停用词、unicode61 行为 |
| `tests/test_ingestion_worker_app.py` | `TestClient` 调 `/internal/parse`，mock EmbeddingProvider |

### 10.2 集成测试

| 文件 | 覆盖点 |
|---|---|
| `tests/test_knowledge_api_m1.py`（重写） | 上传→解析→ready→published→问答全流程；ACL 越权不返回；chunks 列表；feedback 写入。SQLite fixture。 |
| `tests/test_upload_to_publish_flow.py` | multipart + 等待 worker 回写 + publish + query 命中 |
| `tests/test_parse_failure_flow.py` | worker 失败 → `parse_failed` → `/reparse` 恢复 |
| `tests/test_embedding_degraded.py` | OpenAI 401 → 自动降级 stub，标注 degraded |
| `tests/test_internal_auth.py` | 缺失/错误 token 调 `/internal/*` → 401 |

### 10.3 端到端

| 文件 | 覆盖点 |
|---|---|
| `tests/test_two_process_e2e.py` | `subprocess` 启 worker + api，验证 trace_id 跨服务可关联（CI 可选） |

### 10.4 关键 fixture

```python
@pytest.fixture
def knowledge_workspace(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True)
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KNOWLEDGE_SQLITE_PATH", str(data_dir / "knowledge.sqlite3"))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("KNOWLEDGE_API_INTERNAL_TOKEN", "test-token")
    return data_dir
```

### 10.5 不在 M1 测试范围

- 真实 PDF/Word 解析
- 真实 OpenAI 接口
- 性能压测（推迟到 M2 评测）
- K8s/Helm 部署

## 11. 实施拆分（建议执行顺序）

1. 建 `packages/common-schemas`，把现有 Pydantic 抽出来共享。
2. 写 `services/ingestion-worker` 骨架（`/health`、stub embedding、stub parser）。
3. 写 `services/knowledge-api/store.py`（SQLiteStore + schema 初始化）。
4. 把现有 `app.py` 改为依赖 `SQLiteStore`，保留现有 API 路径。
5. 重写 `tests/test_knowledge_api_m1.py` 改用 SQLite fixture，验证行为一致。
6. 加 multipart 上传 + worker_client，引入新状态机。
7. 写 worker 的 markdown/html/text 解析器 + chunker。
8. 加 FTS5 召回 + 简单向量重排到 `retrieval.py`。
9. 接入 OpenAI-compatible EmbeddingProvider。
10. 加 `/reparse` `/archive` `/restore` + `parse_failed` 流程。
11. 补齐错误处理、安全校验、`.env.example`。
12. 跑通本地双进程（`uvicorn` × 2）+ 端到端测试。

## 12. 验收

- `python -m pytest` 全部通过。
- 端到端：上传一个 Markdown SOP → 等待 `ready` → 发布 → 提问命中 → 引用正确。
- ACL 越权：用户 scope 不含 `wireless` 时查询 `wireless` 文档返回 404。
- Worker 失败：worker 模拟 500 → 文档停在 `uploaded` 或转 `parse_failed`，可重试。
- 内存旧实现已完全移除（无 `InMemoryKnowledgeStore` 残留）。
