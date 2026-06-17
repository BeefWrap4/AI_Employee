# Spec — Knowledge M2.3 ACL 复杂策略 + db 错误码

日期：2026-06-17  
主题：M2 阶段二第三轮——chunk 级 ACL、多 scope 表达式、引用二次校验、db 错误码  
适用范围：`services/knowledge-api`、`packages/common-schemas`

## 1. 背景

M1 实现了基础 ACL（`acl_tags ∪ metadata.values` 与 scope 求交），spec §9.4 与 §9.1 列出多项进阶目标未落地。本轮 M2.3 集中处理四件：

- **chunk 级 ACL**：当前 chunk 继承 doc，但无独立覆盖。spec §4.3 提到按"权限、专业线、厂家、版本"过滤，chunk 级控制是落地基础。
- **多 scope 表达式**：当前仅 `scope: list[str]`（AND 语义），无法表达"wireless 或 5g"这类 OR 关系。
- **引用二次校验**：retrieval 阶段过滤 scope，但最终答案 citations 可能在边界条件下越权（中间态变化、逻辑漏洞）。spec §8.3 提到"答案生成阶段再次校验引用片段权限"。
- **db 错误码**：spec §9.1 列出 `db_write_failed` / `index_corrupted` / `db_locked` 三错误码，当前 store 裸跑 `OperationalError` 冒泡 500 无语义。

M2.3 范围：四件全做，闭环 M2。M3 平台（用户/角色/SSO）后续在 store 之上构建。

## 2. 目标

- `chunks` 表新增 `acl_tags_json TEXT NOT NULL DEFAULT '[]'`，写时默认继承 document.acl_tags。
- 新增 `QueryRequest.knowledge_scopes_or: list[str]`，OR 语义；与 `scope` 联合：可见集 = 与 `(scope ∪ scope_or)` 有交集。
- `/chat/query` 在生成 citations 时**再次**过 `resolve_visible_docs`，保证不返回越权引用。
- `SQLiteStore` 写方法被 `_with_db_errors` 装饰：`OperationalError("locked")` → 3 次指数退避后 500 `db_locked`；其他 `OperationalError` / `IntegrityError` → 500 `db_write_failed`。
- 启动期 `init_schema` 检测 FTS5 损坏：抛 `IndexCorruptedError`，`create_app` 立即 sys.exit(1)。
- 全量 170 个现有测试保持通过；新增 ≥ 12 个 M2.3 测试。

## 3. 非目标

- 用户/角色/SSO 鉴权：M3 平台。
- 复杂 DNF 表达式解析（仅 AND+OR 两参数；不引入 lexer）。
- SQLite WAL 模式或锁竞争调优。
- 错误响应 `warn` 字段（spec 暂不引入）。
- 真实 SQLite 多进程锁竞争测试：mock 足够。

## 4. 仓库与模块布局

```
AI_Employee/
├─ packages/common-schemas/
│  └─ src/ai_employee/common_schemas/
│     └─ acl.py                # 新增：resolve_visible_docs 纯函数
├─ services/knowledge-api/
│  └─ src/ai_employee/knowledge_api/
│     ├─ store.py                # 修改：chunks 新增 acl_tags_json + ALTER 迁移 + 启动 FTS5 探活 + _with_db_errors 装饰器
│     ├─ schemas.py              # 修改：QueryRequest 加 knowledge_scopes_or
│     ├─ retrieval.py            # 修改：调 resolve_visible_docs + chunk 级过滤 + 引用二次校验
│     └─ app.py                  # 修改：scope_or 接收 + 错误码透传 + IndexCorruptedError 让 create_app 启动失败
├─ tests/
│  ├─ test_chunk_acl.py          # 新增
│  ├─ test_scope_or.py           # 新增
│  ├─ test_citation_recheck.py   # 新增
│  └─ test_db_error_codes.py     # 新增
```

## 5. chunks schema 迁移

### 迁移流程（`init_schema`）

```python
def init_schema(self) -> None:
    with self._lock, self._connect() as conn:
        conn.executescript(_SCHEMA)
        # 增量迁移：chunks 新增 acl_tags_json
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
        if "acl_tags_json" not in cols:
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN acl_tags_json TEXT NOT NULL DEFAULT '[]'"
            )
        conn.commit()
    # 启动期 FTS5 探活
    try:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM chunks_fts LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        raise IndexCorruptedError(f"FTS5 索引损坏: {exc}") from exc
```

### 兼容性

- 旧库：ALTER TABLE 加 `acl_tags_json='[]'`，老 chunks 视作继承 doc。
- 新库：`_SCHEMA` 中 `chunks` 表本身已含 `acl_tags_json TEXT NOT NULL DEFAULT '[]'`。
- `init_schema` 用 `CREATE TABLE IF NOT EXISTS` + 增量 ALTER，新老库都走通。

### write_chunks 扩展

```python
def write_chunks(
    self,
    doc_id: str,
    chunks: list[dict],
    embeddings: list[list[float]],
    embedding_model: str,
    acl_tags_override: list[str] | None = None,  # 新增
) -> None:
    """acl_tags_override 为 None 时从 documents.acl_tags 继承。
    非 None 时用提供的列表（手动覆盖）。
    """
    if acl_tags_override is None:
        doc = self.get_document(doc_id)
        acl_tags = doc.get("acl_tags", [])
    else:
        acl_tags = list(acl_tags_override)
    acl_json = json.dumps(acl_tags, ensure_ascii=False)
    # 写入时附带 acl_tags_json
```

### 新增 store 方法

```python
def get_chunk(self, chunk_id: str) -> dict | None
def set_chunk_acl_tags(self, chunk_id: str, acl_tags: list[str]) -> None
```

## 6. 多 scope 表达式

### `common_schemas.acl.resolve_visible_docs`

```python
def resolve_visible_docs(
    store: "SQLiteStore",
    scope: list[str] | None,
    scope_or: list[str] | None,
) -> list[str]:
    """计算 doc_id 列表。规则：
      - 文档需 published 状态
      - documents.acl_tags ∪ metadata.values 与 (set(scope) | set(scope_or)) 有交集
      - scope 与 scope_or 都为空 → 返回所有 published
      - 返回按 doc_id 排序
    """
```

**算法**：
- `effective_scopes = set(scope or []) | set(scope_or or [])`
- 若 `effective_scopes` 为空 → `store.list_documents(status="published")` 返回的 doc_ids
- 否则对每个 published doc 计算 `visible = doc.acl_tags ∪ set(metadata.values())`，过滤 `bool(visible & effective_scopes)`

向后兼容：旧代码直接调 `store.list_published_doc_ids_in_scope(scope)`，新代码改调 `resolve_visible_docs(store, scope, scope_or)`。后者把 `list_published_doc_ids_in_scope` 废弃（保留兼容旧测试，函数标记 `deprecated`）。

### `QueryRequest` 扩展

```python
class QueryRequest(BaseModel):
    session_id: str
    question: str
    knowledge_scopes: list[str] = Field(default_factory=list)
    knowledge_scopes_or: list[str] = Field(default_factory=list)  # 新增 OR
    stream: bool = False
```

向后兼容：`knowledge_scopes_or` 默认 `[]`，旧调用行为不变。

## 7. chunk 级过滤 + 引用二次校验

### chunk 级过滤（retrieval 阶段）

`RetrievalService.search` 流程：
1. `doc_ids = resolve_visible_docs(...)`
2. FTS5 / 向量召回从 `doc_ids` 候选中拿 chunks
3. 对每条 chunk，**再过 chunk 级 ACL**：`chunks.acl_tags_json == '[]'` 视为继承 doc（已在 step 1 通过 doc 级过滤即允许）；非 `[]` 时检查 chunk.acl_tags 与 effective_scopes 求交
4. 不通过的 chunk 从候选丢弃

实现位置：FTS5 SQL 拼接时加 chunk 级 WHERE：
```sql
WHERE chunks_fts MATCH ? AND c.doc_id IN (...) 
  AND (c.acl_tags_json = '[]' OR EXISTS (
    SELECT 1 FROM json_each(c.acl_tags_json) 
    WHERE json_each.value IN (...effective_scopes...)
  ))
```

为简化 M2.3，先在 Python 端做 chunk 级过滤（store 返回 candidates，retrieval 二次过滤）；SQL 优化推后续。

### 引用二次校验

`/chat/query` 在生成 `citations` 之前，再过一遍 `resolve_visible_docs`：
```python
allowed = set(resolve_visible_docs(store, scope, scope_or))
hits = [h for h in hits if h.doc_id in allowed]
if not hits:
    raise HTTPException(404, {"error_code": "no_knowledge_in_scope"})
```

`Citation` doc_id 必须来自二次校验后的 hits 列表。不通过直接 404（避免返回越权引用）。

## 8. db 错误码

### 装饰器

```python
import functools, time, sqlite3
from fastapi import HTTPException, status


def _with_db_errors(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                return fn(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "locked" in msg or "busy" in msg:
                    if attempt < 2:
                        time.sleep(0.1 * (2 ** attempt))
                        continue
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={"error_code": "db_locked", "message": str(exc)},
                    ) from exc
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error_code": "db_write_failed", "message": str(exc)},
                ) from exc
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error_code": "db_write_failed", "message": str(exc)},
                ) from exc
        # 不可达
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "db_locked", "message": str(last_exc)},
        )
    return wrapper
```

### 包装的写方法

- `create_document`
- `transition_status`
- `mark_parse_failed`
- `write_chunks`
- `set_source_uri`（已被 path_guard 包一层；装饰器在外层）
- `set_chunk_acl_tags`
- `write_qa_log`
- `write_feedback`

读方法不包（读失败应 500，但 error_code 不强制）。

### `IndexCorruptedError`

`packages/common-schemas/acl.py` 与 `services/knowledge-api/store.py` 共用 `IndexCorruptedError`：
- 定义在 `common_schemas.errors`（新文件）
- `init_schema` 探活失败时抛
- `create_app` 捕获后 `sys.exit(1)`

### `create_app` 启动失败行为

```python
def create_app(...):
    try:
        store.init_schema()
    except IndexCorruptedError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    ...
```

`init_schema` 探活在 conftest 之类的测试中如果走 `init_schema` 而 `var` 是临时目录，FTS5 不会损坏，正常通过。

### 错误响应

```json
{"detail": {"error_code": "db_write_failed", "message": "INSERT failed: ..."}}
{"detail": {"error_code": "db_locked", "message": "database is locked"}}
{"detail": {"error_code": "index_corrupted", "message": "FTS5 索引损坏: ..."}}
```

HTTP 500。

## 9. 测试策略

### 单元测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_chunk_acl.py` | chunks 表 acl_tags_json 列存在；ALTER 迁移幂等（重跑 init_schema 不报错）；write_chunks 接受 acl_tags_override；`[]` 视为继承 doc；非空 ACL 与 scope 求交；get_chunk / set_chunk_acl_tags |
| `tests/test_scope_or.py` | `resolve_visible_docs`：仅 scope 命中、仅 scope_or 命中、scope AND scope_or（都需）、两者都空返回全部；与 metadata.values 求交；空 ACL doc 不在非空 scope 命中 |
| `tests/test_citation_recheck.py` | mock retrieval 候选含越权 doc → citations 重过滤后越权被丢；正常候选不受影响；全越权时返回 404（不再返回低质量） |
| `tests/test_db_error_codes.py` | OperationalError("locked") 重试 3 次后 500 `db_locked`；其他 OperationalError → 500 `db_write_failed`；IntegrityError → `db_write_failed`；FTS5 损坏 → IndexCorruptedError；create_app 启动 sys.exit(1) |

### 集成测试（最小新增）

- `tests/test_knowledge_api_m1.py` 增加 `test_chat_query_with_scope_or` 1 条。
- 现有 7 条 ACL/查询测试保持通过（向后兼容）。

### 回归

- 全量 170 + ≥ 12 新增 = ≥ 182 测试通过。
- 端到端：Qwen + 真实服务 + scope_or 命中 → 200；scope + scope_or 都空 → 全 published；任一 db 错误注入 → 500 + 正确 error_code。

### 不在 M2.3 测试范围

- 真实多进程 SQLite 锁竞争。
- 复杂 DNF 表达式。
- chunks.acl_tags 手动覆写 UI。

## 10. 验收

- `python -m pytest` 全部通过（170 + ≥ 12）。
- 上传 doc → 写 chunks 时 `acl_tags_json` 继承 document.acl_tags，存到 DB。
- 端到端：`/chat/query` 接收 `knowledge_scopes_or=["5g"]`，与 `knowledge_scopes=["wireless"]` 求并集，命中"network_type=5g" 或 acl_tags=wireless 的 doc。
- 端到端：mock 越权 doc 进入候选 → 最终 citations 0 条 → 404 `no_knowledge_in_scope`。
- 端到端：mock sqlite OperationalError("locked") → 上传 3 次重试后 500 `db_locked`。
- 端到端：临时破坏 chunks_fts → `create_app` 立即 sys.exit(1) 含 `index_corrupted` 提示。

## 11. 实施拆分（建议执行顺序）

1. `common_schemas.errors.IndexCorruptedError` + `common_schemas.acl.resolve_visible_docs` 纯函数 + 单测。
2. `SQLiteStore._with_db_errors` 装饰器 + 包装所有写方法 + `db_write_failed` / `db_locked` 单测。
3. `init_schema` chunks.acl_tags_json ALTER 迁移 + FTS5 探活 + `create_app` 启动失败 + 单测。
4. `SQLiteStore.write_chunks` 接受 `acl_tags_override` + 新增 `get_chunk` / `set_chunk_acl_tags` + 单测。
5. `QueryRequest.knowledge_scopes_or` + `retrieval.search` 调 `resolve_visible_docs` + chunk 级 Python 端过滤 + 单测。
6. `/chat/query` 引用二次校验 + 1 条 M1 集成测试。
7. 跑全量测试 + 端到端：Qwen + scope_or 验证。

---

**与现有 M1/M2.1/M2.2 的关系**：

- M1 已建 `chunks` 表与 6 态状态机；本 spec 增量加列不破坏。
- M2.1 评测依赖 `list_documents` 与检索结果；本 spec 的 chunk 级过滤让 eval 的命中统计更精确（越权 chunk 不再误算 hit）。
- M2.2 path_guard 与 503 降级已实施；本 spec 错误码不冲突——db 错误码覆盖 OperationalError/IntegrityError，与 M2.2 的 embedding/embedding_unavailable 错误码正交。
