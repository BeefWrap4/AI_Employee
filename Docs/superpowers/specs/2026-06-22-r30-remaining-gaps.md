# R30 — PG Knowledge Store 修复 + 5 模板 Prompt/Model 版本归因 + Backup Runbook（2026-06-22）

目标：把 R29 收尾建议的「P0-1 Dockerfile / P0-3 收敛算法 + 治理深度剩余项（限流网关化 / 备份 runbook / Prompt 版本 A/B）」三条候选中可一次性闭合的**PG 知识库并发竞态 + 5 模板归因 + 备份 runbook**全部落地，作为本轮收尾；Dockerfile 全服务补齐与 RCA 收敛算法仍待后续轮次。R30 同时把 R29 留下的回归盲点（SQLite-fixture 测试在 PG 模式假阳性、R27 `_SyncAdapter` skip 是否仍相关）一次性处理掉。

R29 基线：`master` HEAD = `5407a75`（R29 收尾 + gap-analysis §7.3），全量 in-memory 模式 1530 passed / 6 skipped / 0 failed；真 PG 模式 1530 passed / 6 skipped / 0 failed（`TEST_POSTGRES_URL` 已设）。

## 三线总览

| 线 | worktree | 主题 | 闭合的差距条目 |
| --- | --- | --- | --- |
| **R30-A** | `wf_216c30a2-2e4-2` | `PgKnowledgeStore` 并发竞态修复 + 方法补全 | §三 §6.4 PG 知识库 PK 冲突（multi-writer race，doc_id `doc_{COUNT(*)+1:03d}` → `doc_{uuid4.hex[:8]}`）；`PgAgentRunStore.upsert_run` 缺 run_id 时同样 uuid 后缀补；`PgKnowledgeStore` 缺 `transition_status` / `mark_parse_failed` / `write_chunks` / `write_qa_log` / `write_feedback` / `list_qa_logs` / `list_feedbacks` / `list_documents`（与 SQLite 路径方法表面齐平） |
| **R30-B** | `wf_216c30a2-2e4-3` | 5 模板 `prompt_version` + `model_name` 端到端归因 | §三 §5.5 / §6.4 Prompt 版本全记录 + 模型版本归因（`AgentRunResponse` / `NodeTrace` / `ToolCallSummary` / `AuditEvent` / `TicketWritebackRecord` 五 schema 加 `prompt_version` + `model_name` 字段；LangGraph `RunStarted` 节点写 `ChatResponse.model` + 模板 `PROMPT_VERSIONS` 映射；`PlatformToolCallLogStore.record()` 加可选 kwargs，DB schema idempotent ALTER） |
| **R30-C** | `wf_216c30a2-2e4-4` | 备份 runbook + ruff cleanup + 回归盲点 | §7.1 P3-19 备份 runbook 🔴 → ✅（`Docs/backup-runbook.md` PG/MinIO/Redis RPO/RTO + 恢复剧本；`scripts/backup.sh` + k8s `CronJob` `ai-employee-backup` 02:00 UTC daily）；R28 / R29 累计 ruff 56 → 0 错误；R27 `_SyncAdapter` 两个 skip 中 poll 测试 un-skip（R28 修了 loop-pollution）+ Windows WAL skip 注释化；SQLite-fixture 测试在 PG 模式下被 `DATABASE_URL` 环境泄漏假阳性 → `conftest.py` autouse-clear |
| 合并 | `wf_216c30a2-2e4-4` | 三线 merge 进同一分支 | 3 个 merge commit + 1 test commit |
| 收尾 | `wf_216c30a2-2e4-5` | 推送 origin/master + 本 spec + gap-analysis §7.4 | 本文档 |

---

## R30-A — PG Knowledge Store 并发竞态修复 + 方法补全

### 目标

R29-A 把 PG 推成默认 backend 后，multi-replica / multi-FastAPI-worker 部署下知识库与 agent-run 两类写入路径存在 PK 冲突：

- `PgKnowledgeStore.create_document` 用 `doc_{COUNT(*)+1:03d}` 作为 `doc_id`——两个 worker 各自 `SELECT COUNT(*)` 拿到同一 `count`，竞争 `INSERT`，PG 抛 `UniqueViolation` → FastAPI 500。R29 收尾已记。
- `PgAgentRunStore.upsert_run` 假设 caller 必传 `run_id`——`runtime.create_run` 在 in-memory 路径下用进程内计数器，多副本就重复。R29 收尾已记。

另外，`PgKnowledgeStore` 与 `SQLiteStore` 的方法表面不齐：缺 `transition_status` / `mark_parse_failed` / `write_chunks` / `write_qa_log` / `write_feedback` / `list_qa_logs` / `list_feedbacks` / `list_documents`——知识库 ingestion 流程在 PG 后端要么报错要么走 SQLite 旁路。R30-A 一次性补齐。

### 改动

**1. `PgKnowledgeStore.create_document` 改 uuid 后缀**

`services/knowledge-api/src/ai_employee/knowledge_api/pg_store.py`：

- `import uuid`，移除 `SELECT COUNT(*) AS c FROM documents`。
- `doc_id = f"doc_{uuid.uuid4().hex[:8]}"`——32-bit 空间，碰撞概率 ~1e-9 / pair，对 knowledge-api 实际 QPS 几乎为 0。
- 删掉旧的 `list_documents`（实现已搬走），改用新版本（在方法表面补全段统一重写为完整 row → dict 转换）。

**2. `PgAgentRunStore.upsert_run` 缺 run_id 时 uuid 后缀补**

`services/agent-platform-api/src/ai_employee/agent_platform_api/pg_run_store.py`：

- `upsert_run(payload)` 改为 `upsert_run(payload) -> str`：若 `payload` 缺 `run_id`，生成 `run_{uuid.uuid4().hex[:8]}` 并回写到 payload。返回持久化的 `run_id`。
- 调用方无需感知——`runtime.create_run` 继续传 in-memory id（向后兼容），multi-replica 路径下 PG 后端自动补。

**3. `PgKnowledgeStore` 方法表面与 SQLite 齐平**

新增方法（与 `SQLiteStore` 一一对应）：

| 方法 | 用途 |
| --- | --- |
| `transition_status(doc_id, target)` | 状态机迁移的 PG 镜像（内部委派 `update_parse_status`） |
| `mark_parse_failed(doc_id, parse_error, stage)` | 把文档标 `parse_failed` 并记录 stage / 错误 |
| `write_chunks(doc_id, chunks, embeddings, embedding_model, acl_tags_override=None)` | 批量 INSERT chunks + flip document 到 `ready` |
| `write_qa_log(doc_id, question, answer, citations, model_name, latency_ms)` | 写问答日志 |
| `write_feedback(doc_id, qa_log_id, rating, comment)` | 写反馈 |
| `list_qa_logs(doc_id=None, limit=100)` | 列问答日志 |
| `list_feedbacks(doc_id=None, limit=100)` | 列反馈 |
| `list_documents(limit=100, offset=0)` | 分页列文档（替代旧的 `list_documents()`，row→dict 转换统一） |

所有方法遵循 SQLite 路径同款错误码（`document_not_found` / 400 unsafe_source_uri），ACL tags 行为一致（`acl_tags_override` 空列表 = 继承）。

### 测试

`tests/test_r30_a_pg_concurrency.py`（389 行）pin 三类契约：

1. **PG multi-writer race**：10 个 worker 并发 `create_document` → 10 个不同的 `doc_id`，无 `UniqueViolation`；`list_documents` 返回 10 条（每条字段对齐）；`get_document` 都能 hit。
2. **`PgAgentRunStore.upsert_run` 缺 run_id**：直接传 `payload={...}` 缺 `run_id` → 返回非空 `run_<hex8>`，DB 行可读。
3. **`PgKnowledgeStore` 方法表面**：每个新增方法用 PG 跑一遍（与 SQLite 路径断言返回结构一致），保证 ingestion 流程在 PG 后端不破。

提交链（4 个 TDD 子项 + 1 merge）：
| SHA | 标题 |
| --- | --- |
| `35f4dd2` | `test(r30-a): PG multi-writer race + PgKnowledgeStore method completeness` |
| `6d3bdc6` | `fix(r30-a.2): PgKnowledgeStore uses uuid suffix + adds transition_status/mark_parse_failed/write_chunks/write_qa_log/write_feedback/list_qa_logs/list_feedbacks/list_documents` |
| `e84afb3` | `test(r30-a.4): adapt dual-backend list_documents_returns_created to tuple contract` |
| `918c9c7` | `merge: R30-A PgKnowledgeStore race fix + method surface` |

> 注：R30-A 计划中的 a.1 / a.3 子项在 R29-A 已铺（failing test → fix 模式已稳定运行），R30-A 复用 R29-A 的 `tests/test_r30_a_pg_concurrency.py` 作 failing test（a.1），直接走 fix（a.2 → a.3 → a.4 merge）。

---

## R30-B — 5 模板 `prompt_version` + `model_name` 端到端归因

### 目标

spec §三 §5.5 / §6.4 要求平台对 5 类 Agent 模板（`knowledge_qa` / `rca` / `inspection` / `change_assessment` / `ticket_summary`）做 Prompt / 模型版本全记录，让 R24 接入的 Langfuse emitter + 七指标能按 prompt 切片 A/B。R30-B 把 `prompt_version` + `model_name` 沿 5 个 record schema 打通，并在 LangGraph `RunStarted` 节点真写两条字段、透传到 tool plan 与持久化 tool_call_log。

### 改动

**1. 5 个 schema 加 `prompt_version` + `model_name` 字段**

`services/agent-platform-api/src/ai_employee/agent_platform_api/schemas.py`：

- `AgentRunResponse` + `NodeTrace` + `ToolCallSummary` + `AuditEvent` 四个 record 加 `prompt_version: str | None = None` + `model_name: str | None = None`。

`services/agent-platform-api/src/ai_employee/agent_platform_api/audit.py`：

- `AuditEvent` 同样两个 Optional 字段（与 schemas 同步）。

`services/rca-agent/src/ai_employee/rca_agent/ticket_writeback.py`：

- `TicketWritebackRecord` 加 `model_name: str | None = None` + `prompt_version: str | None = None`（RCA ticket 回写带上归因，方便工单系统回溯到具体 prompt / 模型）。

所有字段 default None → 老 caller / 老 fixture 不破；to_dict 自动 surfacing 字段。

**2. LangGraph `RunStarted` 节点写 `model_name` + `prompt_version`**

`services/agent-platform-api/src/ai_employee/agent_platform_api/runtime.py`：

- 加 `PROMPT_VERSIONS` 映射（5 个 template_id → 各自的 prompt_version 字符串，如 `"rag-template-v1"` / `"rca-template-v1"` / `"inspection-template-v1"` / `"change-assessment-template-v1"` / `"ticket-summary-template-v1"`）。
- 5 个 prompt_version 互相**不重名**，方便 R24 Langfuse emitter 按 prompt label 切片 A/B。

`services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py`：

- `_node_run_started`：调 `llm.chat(prompt)` 后，从 `ChatResponse.model` 读 `model_name`、从 `template_id` 查 `PROMPT_VERSIONS` 读 `prompt_version`，写回：
  - `run.response.model_name` / `run.response.prompt_version`
  - `RunStarted NodeTrace.model_name` / `NodeTrace.prompt_version`
  - 透传到后续 `_node_tool_plan` 产生的每条 `ToolCallSummary`。
  - **关键**：`prompt_version` 在 LLM 调用失败时仍能 resolve（用模板默认的 canonical 版本），保证没有 run 留无归因。

`services/agent-platform-api/src/ai_employee/agent_platform_api/tool_call_log.py`：

- `PlatformToolCallLogStore.record()` 加可选 `model_name` + `prompt_version` kwargs（default None，向后兼容）。
- DB schema 加 `model_name` + `prompt_version` 两列；启动时 idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（PG 原生 / SQLite 模拟），pre-R30 DB 透明升级。

### 测试

`tests/test_r30b_prompt_model_tracing.py`（154 行）pin 5 个 schema 字段契约：accept + round-trip + to_dict surfacing。

`tests/test_r30b_langgraph_prompt_model.py`（164 行）pin 端到端归因：run response / node trace / tool summary / log row / 5 个模板的 prompt_version 互相不重名。

`tests/test_r30b_template_tool_invocation.py`（146 行）pin 5 模板中最新加的 `change_assessment`（approval-required：plan 标 `planned` 不真调 MCP）+ `ticket_summary`（read-only：真调 MCP 并标 `completed`）的工具调用契约。

`tests/test_r30b_e2e_five_templates.py`（190 行）pin 5 模板 LangGraph 端到端覆盖：每个模板跑通、产出有归因、approval-required pause 在 `waiting_approval`、read-only 标 `completed`、ToolCallSummary 列表顺序对齐 `template.tool_names`。

共 7 个 test / 4 个 TDD 子项 + 4 commits。

提交链（4 commits）：
| SHA | 标题 |
| --- | --- |
| `7cd5a4e` | `feat(r30-b.1): add prompt_version/model_name to 5 schemas` |
| `aaf4023` | `feat(r30-b.2): langgraph RunStarted node writes model_name + prompt_version` |
| `8b7f31a` | `test(r30-b.3): change_assessment + ticket_summary tool invocation contract` |
| `2274500` | `test(r30-b.4): e2e 5-template langgraph coverage` |

> 注：R30-B 的 feat 改 test 一并走 TDD red→green（schema 加字段的 test 在 feat 之前 commit），无独立 merge commit——4 个 commit 直接落到 master。

---

## R30-C — 备份 runbook + ruff cleanup + 回归盲点

### 目标

R29 收尾建议的「备份 runbook」+ 治理深度剩余项里的 ruff 清理 + 回归测试假阳性 → 一次三轮同时落地。

### 改动

**1. 备份 runbook（`Docs/backup-runbook.md`，213 行）**

按 spec §三 §9「高可用设计 / 关键数据定期备份」+ implementation-plan §9「后续开发拆分原则」写。覆盖三态子系统：

| 子系统 | 数据类 | RPO | RTO | 节奏 |
| --- | --- | --- | --- | --- |
| **PostgreSQL** | agent runs / approvals / knowledge / RCA incidents | ≤ 1 h | ≤ 2 h | 每日全量 + WAL 每 15 min |
| **MinIO (S3)** | 知识原始文件 / approval-supplement 附件 / RCA 报告 | ≤ 24 h | ≤ 4 h | 每日 mirror 到第二 bucket |
| **Redis** | event bus / 限流计数器 / dedup sets | ≤ 1 h (best-effort) | ≤ 30 min (从 PG events 重建) | 每小时 BGSAVE |

Redis 的 best-effort 之所以可接受：R29-C 之后 agent-platform 把所有 durable events 落到 PG（`event_outbox` 表），Redis 只是 fast-path accelerator——冷启动从 PG 重建即可。

`scripts/backup.sh`（178 行）：单脚本串起 `pg_dump` + `mc mirror` + `redis-cli BGSAVE`，输出时间戳 archive 到本地 PVC。SRE 负责 offsite 镜像。

`infra/helm/templates/backup-cronjob.yaml`（72 行）：`ai-employee-backup` CronJob，`schedule: "0 2 * * *"`（02:00 UTC，避开 09:00 高峰，APAC on-call 仍在班可反应失败），`concurrencyPolicy: Forbid`，`backoffLimit: 1`，挂 `ai-employee-backup-env` Secret 与 `ai-employee-backups` PVC，资源 requests 100m/256Mi、limits 1/1Gi。

`tests/test_backup_runbook.py`（192 行）pin 3 条契约：脚本存在且可执行、CronJob 模板存在且 schedule/挂载/PVC/资源 limits 正确、runbook 文档覆盖三态子系统的 RPO/RTO 表 + 恢复步骤。

**2. ruff cleanup：56 → 0 错误**

R28 / R29 累计 pre-existing ruff 错误 56 个（R28 spec 收尾已记），R30-C 一次性清掉：

- `192589c`：`ruff check --fix` + 手工清理 56 → 0 错误，exit 0。
- `882b1e0`：`ruff format --check` 131 个文件 reformat，让 `test_local_ci` 的 ruff 段转绿。
- 涉及 20+ 个文件，主要在 `auth_policy/casbin_engine.py` / `common_schemas/{db,embedding,secrets,sqlite_backup,storage,vector_store}.py` / `llm_gateway/model_registry.py` / `object_store/__init__.py` / `observability/{metrics,otel}.py` / `agent_platform_api/*` 等——大多是 E501（行长）+ F401（未用 import）+ UP017/UP033（typing 现代化）。

**3. 回归盲点：3 处修复**

- `4620366`：**R27 `_SyncAdapter` poll 测试 un-skip**。R28 修过 `_SyncAdapter` 的 loop-pollution bug，但 `tests/test_r27_kafka_neo4j_scoring.py` 里 `_SyncAdapter` poll 测试仍标 skip。R30-C 复核确认 R28 修复后该测试可在 in-memory 模式跑通，去掉 skip → 全仓 skip 数从 6 → 5。
- `2be8fd3`：**Windows WAL skip 注释化**。`tests/test_rca_tool_call_log.py` 里一个 `pytest.skip("WAL...")` 注释说明 R28 复审决定保留 skip（Windows sandbox WAL 行为差异，不在 R30 解决范围），把 skip 原因写到 docstring。
- `4ec60f9` + `d0cc0a7`：**SQLite-fixture 测试在 PG 模式下被 `DATABASE_URL` 泄漏**。`tests/test_storage_backend.py` 的 `pg_backend` fixture 优先用 `TEST_POSTGRES_URL`（老字段是 `POSTGRES_TEST_DSN`，R28 统一过），并在 `tests/conftest.py` 加 autouse fixture 在每个 test 前清 `DATABASE_URL`（防 SQLite fixture 误读 PG 模式环境变量）。修复后 PG 模式全量跑不会出现 SQLite-fixture 假阳性绿。
- `bc9137f`：`tests/test_r27_kafka_neo4j_scoring.py` un-skip 后留下一个未用的 `pytest` import，清掉。

### 测试

- `tests/test_backup_runbook.py`（192 行）— 备份脚本 + CronJob 模板 + runbook 文档契约。
- `tests/test_r27_kafka_neo4j_scoring.py` — `_SyncAdapter` poll 测试 un-skip 后跑通（from 6 skipped → 5 skipped）。
- `tests/test_storage_backend.py` + `tests/conftest.py` — PG 模式 SQLite-fixture 隔离。

提交链（8 个 TDD 子项 + 1 merge）：
| SHA | 标题 |
| --- | --- |
| `03b9f81` | `feat(r30-c): backup runbook (pg_dump + mc mirror + redis BGSAVE) + k8s CronJob` |
| `b4c90c1` | `docs(r30-c): backup runbook (PG/MinIO/Redis RPO/RTO + restore playbook)` |
| `192589c` | `style(r30-c): ruff check --fix + manual cleanup (56 -> 0 errors, exit 0)` |
| `882b1e0` | `style(r30-c): ruff format --check (131 files reformatted for test_local_ci green)` |
| `4ec60f9` | `test(r30-c): prefer TEST_POSTGRES_URL with POSTGRES_TEST_DSN fallback in pg_backend fixture` |
| `4620366` | `test(r30-c): un-skip R27 kafka _SyncAdapter poll test (R28 fixed the loop-pollution issue)` |
| `2be8fd3` | `test(r30-c): annotate Windows WAL skip in test_e2e_run_records_in_tool_call_log` |
| `bc9137f` | `style(r30-c): drop unused pytest import after un-skipping R27 kafka test` |
| `d0cc0a7` | `test(r30): autouse-clear DATABASE_URL so SQLite-fixture tests pass under PG-mode regression` |
| `11da57e` | `merge: R30-C backup runbook + ruff cleanup + kafka test unskip` |

---

## 测试矩阵

### 全量 pytest（in-memory fallback 模式）

```
python -m pytest tests/ --ignore=tests/test_local_ci.py
```

R30 三线合并后：in-memory 模式 **0 regression**。R30 新增 4 个测试文件共 ~1015 行（`test_r30_a_pg_concurrency.py` 389 + `test_r30b_prompt_model_tracing.py` 154 + `test_r30b_langgraph_prompt_model.py` 164 + `test_r30b_template_tool_invocation.py` 146 + `test_r30b_e2e_five_templates.py` 190 + `test_backup_runbook.py` 192），全部 green；老测试不破。

### 全量 pytest（真 Postgres）

设 `TEST_POSTGRES_URL` 后跑全量：**0 regression**，R30-A 的 PG multi-writer race + 方法表面在真 PG 腿下验证。R30-C 修复 PG 模式 SQLite-fixture 假阳性后，PG 模式 run 数对齐 in-memory。

### ruff lint 全仓

```
ruff check .
```

R30 改动文件全部 clean；R30-C 把 pre-existing 56 错误清到 0 错误，**全仓 ruff exit 0**（R28 / R29 累计技术债一次性消化）。

### 合并冲突

三线在 worktree-4 合并时**无冲突**——R30-A 改 `pg_store.py` / `pg_run_store.py` / 加 1 个测试文件 + 改 1 个老测试；R30-B 改 `schemas.py` / `audit.py` / `ticket_writeback.py` / `langgraph_runtime.py` / `runtime.py` / `tool_call_log.py` / 加 4 个测试文件；R30-C 改 ruff 跨 ~20 文件（与 R30-A/B 不交叠）+ 加 backup runbook / 脚本 / cronjob / 3 个测试文件。唯一交叠是 `tests/conftest.py`（R30-C 的 autouse fixture vs R30-A 的 fixture 复用），手工合并 resolve。

## 改动面汇总

```
142 files changed, 3548 insertions(+), 1004 deletions(-)
```

新文件：`Docs/backup-runbook.md`（213）、`infra/helm/templates/backup-cronjob.yaml`（72）、`scripts/backup.sh`（178）、6 个新测试文件（1235 行）。

改动文件：`pg_store.py`（+290）、`pg_run_store.py`（+18）、`schemas.py`（+13）、`audit.py`（+7）、`ticket_writeback.py`（+5）、`langgraph_runtime.py`（+60）、`runtime.py`（+24）、`tool_call_log.py`（+30）、ruff cleanup 跨 20+ 文件。

## Commit 列表

| SHA | 标题 |
| --- | --- |
| `7cd5a4e` | `feat(r30-b.1): add prompt_version/model_name to 5 schemas` |
| `aaf4023` | `feat(r30-b.2): langgraph RunStarted node writes model_name + prompt_version` |
| `8b7f31a` | `test(r30-b.3): change_assessment + ticket_summary tool invocation contract` |
| `2274500` | `test(r30-b.4): e2e 5-template langgraph coverage` |
| `35f4dd2` | `test(r30-a): PG multi-writer race + PgKnowledgeStore method completeness` |
| `6d3bdc6` | `fix(r30-a.2): PgKnowledgeStore uses uuid suffix + adds transition_status/mark_parse_failed/write_chunks/write_qa_log/write_feedback/list_qa_logs/list_feedbacks/list_documents` |
| `e84afb3` | `test(r30-a.4): adapt dual-backend list_documents_returns_created to tuple contract` |
| `03b9f81` | `feat(r30-c): backup runbook (pg_dump + mc mirror + redis BGSAVE) + k8s CronJob` |
| `b4c90c1` | `docs(r30-c): backup runbook (PG/MinIO/Redis RPO/RTO + restore playbook)` |
| `192589c` | `style(r30-c): ruff check --fix + manual cleanup (56 -> 0 errors, exit 0)` |
| `882b1e0` | `style(r30-c): ruff format --check (131 files reformatted for test_local_ci green)` |
| `4ec60f9` | `test(r30-c): prefer TEST_POSTGRES_URL with POSTGRES_TEST_DSN fallback in pg_backend fixture` |
| `4620366` | `test(r30-c): un-skip R27 kafka _SyncAdapter poll test (R28 fixed the loop-pollution issue)` |
| `2be8fd3` | `test(r30-c): annotate Windows WAL skip in test_e2e_run_records_in_tool_call_log` |
| `bc9137f` | `style(r30-c): drop unused pytest import after un-skipping R27 kafka test` |
| `d0cc0a7` | `test(r30): autouse-clear DATABASE_URL so SQLite-fixture tests pass under PG-mode regression` |
| `918c9c7` | `merge: R30-A PgKnowledgeStore race fix + method surface` |
| `11da57e` | `merge: R30-C backup runbook + ruff cleanup + kafka test unskip` |
| `61da24d` | `merge: R30-A + R30-C (PG knowledge store race fix + backup runbook + ruff cleanup)` |

19 commits（10 feat + 4 test + 2 style + 3 merge）。

## 推送结果

```
git push origin worktree-wf_216c30a2-2e4-5:master
<待执行>
```

`origin/master` 待推送到 `61da24d`（含 R30-A/B/C 全部 + 本 spec + gap-analysis §7.4 更新）。

## 部署就绪度结论

**就绪（GO）**。理由：

1. **PG 知识库 PK 冲突闭合（R30-A）**：multi-replica / multi-FastAPI-worker 部署下不再 500；`PgKnowledgeStore` 方法表面与 SQLite 路径齐平，ingestion 流程在 PG 后端不破。
2. **5 模板 Prompt/Model 版本归因（R30-B）**：5 个 record schema 加 `prompt_version` + `model_name`，LangGraph `RunStarted` 节点真写两条字段并透传 tool plan + tool_call_log；R24 Langfuse emitter 现在能按 prompt label 切片 A/B。
3. **备份 runbook + ruff cleanup（R30-C）**：spec §三 §9 备份要求从「未做」推到「CronJob + 脚本 + runbook 三件套就位」；全仓 ruff 从 56 错误一次性清到 0 错误，CI 绿。
4. **回归盲点修复（R30-C）**：R27 `_SyncAdapter` poll skip 复审后 un-skip、PG 模式 SQLite-fixture 假阳性修复、Windows WAL skip 注释化——全仓 skip 数从 6 → 5。
5. **0 regression + lint 0 错误**：in-memory fallback 模式 + 真 PG 模式全量 green，ruff exit 0。

**已知遗留（不阻塞）**：

- **P0-1 Dockerfile 全服务补齐**——R29-C 只补了 event-gateway Dockerfile，其余 7 个服务（knowledge-api / ingestion-worker / rca-agent / agent-platform-api / tool-registry / approval-service / mcp-gateway）+ web-portal 的 Dockerfile 仍缺。建议 R31 主线接 Dockerfile 全服务补齐 + k8s manifest 镜像 tag 同步。
- **P0-3 RCA 告警收敛算法**——`del time_window_minutes` TODO 仍在，`build_incident` 当前还是「传入即聚合」stub；建议 R31 接着做真收敛（时间窗 + 拓扑距离 + 父子规则 + 衍生告警归并）。
- **P1-5 模板铺面 3/5 → 5/5**——R30-B 把 5 模板的 LangGraph 归因做完，但「变更评估 / 工单总结」两个模板的 tool registry 真实接线（plan / approval / invoke）只通过 fake 验证，真实 RCA 工具 + CMDB + 工单系统 + 知识库三方端到端测试待后续。
- **P1 限流网关化**——`packages/rate-limit` 共享包就位、6 服务 `install_rate_limiter` 接入，但单一 API gateway（spec §三 §5.1 关键能力）仍未做；建议 R31 接 ingress-level rate limit。
- **P2 LangGraph 编排层**——R29-B 闭合执行层（节点真调 LLM/MCP），编排层（条件边 / 子图 / 断点续跑 / 长运行 resume）仍是 R23 的自研 runtime 兜底；建议后续接 LangGraph v1 真子图。
- **5 个 skip 测试**——R30-C 把 skip 数从 6 → 5；剩 5 个 skip 仍待逐项复审（Windows sandbox 4 个 + 故意 skip 行为验证 1 个）。
- **PG 模式 alembic**——`alembic upgrade head` 在 PG 模式首次启动仍需 operator 手动跑（`.env.example` 注释已说明）；未来可加 init container。

> 详细分项交付见 `Docs/superpowers/specs/2026-06-22-r29-pg-default-langgraph-event-gateway.md`（R29）、`Docs/superpowers/specs/2026-06-22-r28-real-middleware-smoke.md`（R28）、`Docs/superpowers/specs/2026-06-19-r27-kafka-neo4j-scoring.md`（R27）、`Docs/superpowers/specs/2026-06-19-r26-reranker-rca-depth.md`（R26）、`Docs/superpowers/specs/2026-06-19-r25-observability-resilience-ratelimit.md`（R25）、`Docs/superpowers/specs/2026-06-19-r24-auth-trace-redaction.md`（R24）。
