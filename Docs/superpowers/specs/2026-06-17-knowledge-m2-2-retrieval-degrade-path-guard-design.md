# Spec — Knowledge M2.2 检索降级与路径校验

日期：2026-06-17  
主题：M2 阶段二第二轮——Qwen 检索降级（503）与 source_uri 路径校验  
适用范围：`packages/common-schemas`、`services/knowledge-api`、`services/ingestion-worker`

## 1. 背景

M2 阶段二第一轮（`Docs/superpowers/specs/2026-06-17-knowledge-m2-eval-and-audit-design.md`）已完成评测与审计。本轮 M2.2 聚焦两件未做的稳定性工作：

- **检索降级**：`/chat/query` 调 query 侧 embedding provider（Qwen/OpenAICompat）失败时，目前会冒泡为 500 Internal Server Error。spec §9.1 明确要求"模型或向量库不可用时，服务返回可解释错误或降级摘要，不生成无证据答案"。
- **路径校验**：spec §9.4 要求"source_uri 路径校验：仅接受 `${KNOWLEDGE_DATA_DIR}/raw/` 下的绝对路径"。M1 审计中标记为缺口，未实施。

M2.2 范围内只做这两件。ACL 复杂策略（chunk 级、二次引用校验、用户/角色映射）与 db 错误码（`db_write_failed` / `index_corrupted` / `db_locked`）推 M3。

## 2. 目标

- `packages/common-schemas/security.py` 提供 `assert_safe_source_uri(path, data_dir)` 纯函数，path_guard 规则完备。
- knowledge-api `SQLiteStore` 写入侧（`set_source_uri`）强制调用 path_guard。
- ingestion-worker `/internal/parse` 读取 `file_path` 前调用 path_guard。
- `QwenEmbeddingProvider` / `OpenAICompatEmbeddingProvider` 在 HTTP 失败且重试用尽后抛 `EmbeddingUnavailableError`（替换原 `RuntimeError`）。
- knowledge-api `RetrievalService.search` 捕获 `EmbeddingUnavailableError` → 503 `embedding_unavailable`，含 trace_id，不写 qa_log。
- 全量 125 个现有测试保持通过；新增 ≥ 8 个 M2.2 测试。

## 3. 非目标

- chunk 级 ACL、引用二次校验、用户/角色模型、SSO：M3 平台。
- 真实网络断网 / 超时 e2e 测试：mock 覆盖足够。
- `db_write_failed` / `index_corrupted` / `db_locked` 错误码：M3。
- `test_two_process_e2e.py`：M2.1 标记的 M1 遗留。
- chunker 合并阈值 15 vs spec 200：M1 遗留。

## 4. 仓库与模块布局

```
AI_Employee/
├─ packages/common-schemas/
│  └─ src/ai_employee/common_schemas/
│     ├─ security.py            # 新增：UnsafeSourceUriError + assert_safe_source_uri
│     └─ embedding.py           # 修改：新增 EmbeddingUnavailableError
├─ services/knowledge-api/
│  └─ src/ai_employee/knowledge_api/
│     ├─ store.py                # 修改：set_source_uri 调 _assert_safe_source_uri
│     └─ retrieval.py            # 修改：捕获 EmbeddingUnavailableError → 503
├─ services/ingestion-worker/
│  └─ src/ai_employee/ingestion_worker/
│     └─ app.py                  # 修改：/internal/parse 解析前调 assert_safe_source_uri
└─ tests/
   ├─ test_path_guard.py              # 新增
   ├─ test_store_path_validation.py   # 新增
   └─ test_retrieval_degraded.py      # 新增
```

## 5. 路径校验

### `common_schemas.security`

```python
class UnsafeSourceUriError(ValueError):
    """source_uri 不符合安全约束。"""

def assert_safe_source_uri(path: str, data_dir: str) -> str:
    """校验 path 位于 data_dir/raw/ 之下且为绝对路径。返回规范化路径。

    规则：
      1. path 必须为绝对路径
      2. data_dir 必须可 resolve 为绝对路径
      3. Path(path).resolve(strict=False) 必须位于 Path(data_dir).resolve()/"raw" 之下
    """
```

### 规则细节

1. `Path(path).is_absolute()` 否则 `raise UnsafeSourceUriError("not absolute")`。
2. `data_dir` 空或 `Path(data_dir).resolve(strict=True)` 失败（如不存在）→ `UnsafeSourceUriError("invalid data_dir")`。
3. `raw_root = Path(data_dir).resolve() / "raw"`；`resolved = Path(path).resolve(strict=False)`；`resolved.is_relative_to(raw_root)` 否则 `raise UnsafeSourceUriError("outside data_dir/raw")`。
4. 返回 `str(resolved)`（规范化后的绝对路径）。

### 边界用例

| 输入 | data_dir | 期望 |
|---|---|---|
| `/tmp/foo/raw/x.md` | `/tmp/foo` | OK（返回 `/tmp/foo/raw/x.md`） |
| `x.md`（相对） | `/tmp/foo` | 拒绝 not absolute |
| `/tmp/foo/x.md` | `/tmp/foo` | 拒绝 outside data_dir/raw |
| `/tmp/foo/raw/../x.md` | `/tmp/foo` | resolve 后 `/tmp/foo/x.md` → 拒绝 outside |
| `/tmp/foo/raw_sub/x.md` | `/tmp/foo` | 拒绝 outside（is_relative_to 严格） |
| symlink `/tmp/foo/raw/link → /etc/passwd` | `/tmp/foo` | resolve 跟随 → `/etc/passwd` → 拒绝 |
| 空 data_dir | — | 拒绝 invalid data_dir |
| `data_dir` 指向不存在路径 | — | 拒绝 invalid data_dir（resolve strict=True 失败） |

### 跨平台

- POSIX：`is_relative_to` 字符串比较。
- Windows：依赖 `Path.resolve()` 在原文件系统下规范化大小写。`is_relative_to` 仍按字符串比较；Win32 文件系统不区分大小写，但 `Path.resolve()` 不强制 lowercase，跨平台语义差异可接受。
- 测试在 Windows + POSIX 都跑，pytest 自动覆盖。

## 6. 检索降级

### 异常层次

`packages/common-schemas/embedding.py`：

```python
class EmbeddingProvider(Protocol):
    name: str
    dim: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingUnavailableError(RuntimeError):
    """远程 embedding provider 调用失败且重试用尽（503 候选）。"""
    def __init__(self, message: str, cause: str = "provider_error") -> None:
        super().__init__(message)
        self.cause = cause  # "network" / "4xx" / "5xx" / "timeout" / "dim_mismatch"
```

### Provider 抛错规则

`QwenEmbeddingProvider.embed` / `OpenAICompatEmbeddingProvider.embed`：
- 4xx（非 429）：不可重试 → 抛 `EmbeddingUnavailableError(message, cause="4xx")`。
- 429 / 5xx：重试用尽 → 抛 `EmbeddingUnavailableError(message, cause="5xx")`。
- httpx `TimeoutException` 重试用尽 → `EmbeddingUnavailableError(message, cause="timeout")`。
- httpx `HTTPError`（网络错误）→ `EmbeddingUnavailableError(message, cause="network")`。
- dim 不匹配 → 仍抛 `EmbeddingUnavailableError(message, cause="dim_mismatch")`。

`StubEmbeddingProvider` 不抛（本地函数）。

### retrieval 翻译为 503

`services/knowledge-api/retrieval.py`：
- `RetrievalService.search` 在 `_embed_question(self.query_provider, question)` 处捕获 `EmbeddingUnavailableError`：
  ```python
  try:
      question_vec = _embed_question(self.query_provider, question)
  except EmbeddingUnavailableError as exc:
      raise HTTPException(
          status_code=503,
          detail={
              "error_code": "embedding_unavailable",
              "message": str(exc),
              "trace_id": ...,
          },
      )
  ```
- **不**写 qa_log（基础设施问题，不污染检索历史）。
- 错误响应含 trace_id（与现有 trace_id 生成逻辑一致）。

### `/health` 互补

- 启动期已知降级（Qwen 缺 key）→ `embedding_provider_degraded=true`（M2 已有）。
- 运行期瞬时失败 → `/chat/query` 503 `embedding_unavailable`。
- 两者职责分明：健康检查报告持久状态，问答报告瞬时故障。

### 错误响应格式

```json
{
  "detail": {
    "error_code": "embedding_unavailable",
    "message": "embedding provider failed: qwen api returned 401",
    "trace_id": "trace_s_q_query_1734512345"
  }
}
```

HTTP 503。

## 7. 测试策略

### 单元测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_path_guard.py` | 8+ 边界用例：绝对/相对/resolve 回退/symlink/大小写/null/空 data_dir；Win32 路径（用 `Path` 构造）；POSIX 路径（用 pytest tmp_path） |
| `tests/test_store_path_validation.py` | `set_source_uri` 接受 raw 路径、拒绝相对路径、拒绝 raw 之外；现有 `set_source_uri` 测试路径仍工作 |
| `tests/test_retrieval_degraded.py` | mock `QueryProvider` 抛 `EmbeddingUnavailableError` → 503 + `embedding_unavailable`、不写 qa_log；stub 路径 200 仍正常；OpenAICompat 抛同异常同样 503 |

### 集成测试（最小新增）

- `tests/test_knowledge_api_m1.py` 增加 1 条 `test_chat_query_returns_503_when_embedding_provider_fails`：用 fake `QueryProvider` 注入 → 503。
- 不破坏现有 125 个测试。

### M1 回归

- 全量 `python -m pytest` 通过（125 已有 + ≥ 8 新增）。

### 不在 M2.2 测试范围

- 真实网络断网 / 超时 e2e。
- ACL 细化、db 错误码、e2e subprocess 测试。

## 8. 验收

- `python -m pytest` 全部通过（125 + ≥ 8 = ≥ 133）。
- 端到端：mock Qwen provider 失败 → `/chat/query` 返回 503 `embedding_unavailable`，响应含 trace_id，qa_log 表无新增。
- 端到端：`set_source_uri` 写入 `../../../etc/passwd` → HTTPException 500 `path_not_allowed`。
- 端到端：ingestion-worker `/internal/parse` 收到 `file_path=/etc/passwd`（绕过 knowledge-api 直接打 worker）→ 400 `path_not_allowed`。
- 全量 8+ 边界 path_guard 测试覆盖，OS 无关测试自动适配 Windows + POSIX。

## 9. 实施拆分（建议执行顺序）

1. `common_schemas.security`：`assert_safe_source_uri` + `UnsafeSourceUriError` + 8 边界单测。
2. `common_schemas.embedding`：`EmbeddingUnavailableError`；`Qwen` / `OpenAICompat` embed 抛此异常替换 RuntimeError；stub 不变。
3. `SQLiteStore._assert_safe_source_uri` 私有方法 + `set_source_uri` 调用；新增单测覆盖写入校验。
4. `RetrievalService.search` 捕获 `EmbeddingUnavailableError` → 503；新增 `test_retrieval_degraded.py` 与 1 条 M1 集成测试。
5. ingestion-worker `/internal/parse` 在解析前调 `assert_safe_source_uri`；失败 → 400 `path_not_allowed`。
6. 跑全量测试 + 端到端：临时设错路径触发 503 与 500 path_not_allowed 验证。

---

**与现有 M1/M2.1 的关系**：M1 spec 已预埋 `source_uri` 路径校验的位置（§9.4），本 spec 给出具体实现。M2.1 评审时记录的"检索降级未做"也由本 spec 闭环。`embedding_provider_degraded` 健康字段（M2.1 新增）与本 spec 的运行时 503 是互补关系，前者报告持久降级，后者报告瞬时故障。
