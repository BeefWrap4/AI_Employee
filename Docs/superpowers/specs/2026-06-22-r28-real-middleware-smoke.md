# R28 — Real-Middleware Full Regression Smoke (2026-06-22)

目标：在真实 Postgres / Redis / MinIO 中间件下跑完全量 pytest 回归 + M1 端到端冒烟，暴露并修复此前一直被 skip 掉的 live-PG 测试缺陷，确认部署就绪度。

## 背景

R17–R27 每轮的回归基线都没有设 `TEST_POSTGRES_URL`，所以所有 `@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"))` 的双后端 live-PG 证明测试一直被跳过。R27 基线是 **1522 passed / 14 skipped**。R28 第一次把真实中间件 env 全部打开跑全量，把这些 live-PG 测试真正跑起来——结果暴露出 2 类一直被 skip 掩盖的真实缺陷。

## 中间件拓扑（验证时实际在跑）

```
docker ps
ai-employee-postgres-1  Up (healthy)  5432   PostgreSQL 16.14
ai-employee-redis-1     Up (healthy)  6379   redis 7
ai-employee-minio-1     Up            9000-9001  MinIO (bucket: ai-employee)
```

Python 侧连通性已逐个验证（psycopg2 / redis-py / boto3 均握手成功）。

激活 env（pytest 运行时）：

| 变量 | 值 | 作用 |
|---|---|---|
| `TEST_POSTGRES_URL` | `postgresql://ai_employee:ai_employee@localhost:5432/ai_employee` | 激活 5 个 dual-backend 测试文件的 live-PG 腿 |
| `OBJECT_STORE_URL` | `http://localhost:9000` | smoke 走真实 MinIO |
| `OBJECT_STORE_ACCESS_KEY` / `OBJECT_STORE_SECRET_KEY` | `minioadmin` / `minioadmin` | MinIO 凭证 |

注意：**不**在 pytest 运行时设 `REDIS_URL`。redis 相关测试（idempotency / leader_election / query_cache）全部用 `monkeypatch.setenv` 自带 unreachable-port 回退路径，不依赖环境变量；而 `test_redis_backend_factory_returns_in_memory_when_unset` 明确断言「`REDIS_URL` 未设时回退内存后端」，如果环境里挂了 `REDIS_URL` 反而会让它拿到 `RedisBackend` 而误判失败。smoke 脚本运行时同理不挂 `REDIS_URL`。

## R28-V1 — 双后端 live-PG 测试隔离修复

`tests/test_rca_platform_dual_backend.py`、`tests/test_platform_run_store_dual_backend.py`：

pre-R28 这两个文件的 `_dbs()` 生成器在 `TEST_POSTGRES_URL` 设了时，会 yield 一个**跨整个文件共享**的 live PG 连接，与每次拿全新 `tmp_path` SQLite 文件的 SQLite 腿形成对照。问题在于 `init_schema()` 是 `CREATE TABLE IF NOT EXISTS`（非破坏性），所以 PG 腿会在文件内累积前序测试的行。一旦把这些测试真正跑起来（而不是 skip），两类缺陷暴露：

1. `test_pg_rca_store_runs_against_live_postgres` 存在断言 bug：它 `save_alarm(_alarm_event("pg-alarm"))`（`alarm_id="pg-alarm"`），却断言 `any(a.alarm_id == "a1" ...)` —— 永远 False。这个测试自 R16-4 写出来就没真正跑过（一直 skip），bug 被静默掩盖。
2. `test_rca_load_empty_returns_empty`（PG 腿）/ platform run store 的 `assert total == 5`、`assert len(events) == 2` 等计数断言看到前序测试留下的脏行，计数漂移。

修复：

- 各加 `_truncate_rca_objects(db)` / `_truncate_run_tables(db)` 辅助函数，在 `_dbs()` yield PG 连接前 `DELETE FROM rca_objects` / `DELETE FROM agent_run_events; DELETE FROM agent_runs` 并 commit。SQLite 腿是 no-op（每次已拿全新 tmp 文件）。这让 PG 腿与 SQLite 腿一样每个测试都从干净状态开始。
- `test_pg_rca_store_runs_against_live_postgres` 的断言改为 `a.alarm_id == "pg-alarm"`（与它实际保存的值一致），并在保存前调一次 `_truncate_rca_objects(db)`。
- `test_pg_run_store_runs_against_live_postgres` 同样加 `_truncate_run_tables(db)`。

`test_knowledge_store_dual_backend.py` 不需要改：它的 `create_document` 每次生成唯一 `doc_<uuid>`，断言用的是 `issubset` + 按 doc_id 取 chunks，对脏 PG 表天然鲁棒（连跑两次 28 passed 验证过）。

## R28-V2 — S3 对象元数据 ASCII 编码修复

`services/knowledge-api/src/ai_employee/knowledge_api/app.py`（上传路径）：

pre-R28 上传文档时把原始 Unicode 标题直接塞进 S3 user-metadata：

```python
_store.put(obj_key, content, content_type=declared_mime, metadata={"title": title})
```

boto3 `put_object` 把 user-metadata 作为 HTTP 头发出，HTTP 头必须是 ASCII。任何非 ASCII 标题（如 smoke 用的「5G RRC 建立失败处理 SOP」）都会触发 `Parameter validation failed: Non ascii characters found in S3 metadata for key "title"`，上传抛异常被 best-effort `except` 吞掉，`obj_key=None`，对象永远没进 MinIO——链路靠本地文件兜底没断，但对象存储路径实际是哑的。

修复：

```python
metadata={"title": title.encode("ascii", "replace").decode("ascii")},
```

Unicode 标题在 DB `documents` 行里仍然完整保留（查询引用 `doc_title` 仍是原文）；S3 metadata 头只是 best-effort 标签，ASCII 替换无损。M1 smoke 对真实 MinIO 现在能成功写入对象，不再有 fallback warning。

## 测试矩阵

### 全量 pytest（真实中间件，`TEST_POSTGRES_URL` 已设）

```
python -m pytest tests/ --ignore=tests/test_local_ci.py
```

| 指标 | R27 基线 | R28 真实中间件 | Δ |
|---|---|---|---|
| passed | 1522 | **1530** | +8 |
| skipped | 14 | **6** | −8 |
| failed | 0 | **0** | 0 |
| 耗时 | — | 90.4s | — |

+8 passed / −8 skipped 的来源：此前 skip 的 8 个 live-PG 测试（5 个 dual-backend 文件里的 `skipif(TEST_POSTGRES_URL)` 用例）现在真正跑起来并全部通过。R28-V1 修掉了其中 2 个一直被 skip 掩盖的缺陷，其余 6 个本就正确只是没跑过。

6 个剩余 skip 全部是已知/预期：

| 文件 | 原因 |
|---|---|
| `test_db_abstraction.py:179` | 该测试本身就是在验证「未设 `TEST_POSTGRES_URL` 时 skip」的行为，故意 `monkeypatch.delenv` 后 `pytest.skip` |
| `test_r27_kafka_neo4j_scoring.py:256,336` | R27 已知遗留：`_SyncAdapter` 后台 asyncio 线程污染全局 loop（已被 inspect-source 测试覆盖） |
| `test_rca_tool_call_log.py:126` | Windows 上 WAL 可见性 flaky（已被直接单测覆盖） |
| `test_storage_backend.py:125,134` | 门控在另一个 env `POSTGRES_TEST_DSN`（非 `TEST_POSTGRES_URL`），未设 |

### M1 端到端冒烟（真实 MinIO）

```
python scripts/m1_smoke.py --json
```

输出（EXIT=0，无 object_store fallback warning）：

```json
{
  "document": {"doc_id": "doc_001", "parse_status": "published", "chunk_count": 2},
  "query": {"trace_id": "trace_smoke_query", "confidence": 0.9981,
            "citation_count": 2,
            "first_citation": {"chunk_id": "chunk_doc_001_001", "doc_id": "doc_001",
                               "doc_title": "5G RRC 建立失败处理 SOP",
                               "page_no": 1, "section_path": "5G RRC 建立失败处理 SOP"}},
  "feedback": {"feedback_id": "fb_001", "trace_id": "trace_smoke_query", "feedback_type": "useful"},
  "audit": {"qa_log_total": 1, "feedback_total": 1}
}
```

上传 → 解析 → 发布 → 查询 → 反馈 全链路绿。对象成功写入 MinIO `ai-employee/documents/` bucket（`mc ls` 可见）。

### ruff lint 全仓

```
ruff check .
```

- 全仓 **61 errors**（pre-existing，master 干净树同样 61，R28 未引入新违规）。
- R28 本次改动的 3 个文件（`tests/test_rca_platform_dual_backend.py`、`tests/test_platform_run_store_dual_backend.py`、`services/knowledge-api/src/ai_employee/knowledge_api/app.py`）**全部 All checks passed**。
- 61 个 pre-existing 错误分布在 `scripts/m1_smoke.py`（E402 import 顺序）、`echarts.py`、`http_resilience.py`、`test_echarts_endpoint.py`、`test_object_store.py`、`test_r27_kafka_neo4j_scoring.py` 等无关文件，属历史技术债，不在本轮验证范围。

## 环境注意事项（给后续轮次）

- **editable install 指向别的 worktree**：`pip install -e .` 是在 `wf_d772724a-24d-1` worktree 里做的，`__editable___ai_employee_0_1_0_finder.py` 的 `MAPPING` 把 `ai_employee.*` 子包硬编码指向那个 worktree 的源码。`python scripts/m1_smoke.py` 直接跑会加载那个 worktree 的旧源码，不是当前 worktree 的改动。pytest 不受影响（`pytest.ini` 的 `pythonpath` 把 `services/*/src` + `packages/*/src` 前置到 `sys.path`，path finder 先于 editable meta-path finder 命中当前 worktree 源码）。要直接跑 smoke/脚本并命中当前 worktree 源码，需显式设 `PYTHONPATH`（Windows 用分号）前置所有 src 目录。
- **不要在 pytest 运行时挂 `REDIS_URL`**：会让 `test_redis_backend_factory_returns_in_memory_when_unset` 拿到 `RedisBackend` 而失败。redis 相关测试自给自足。

## Commit 列表

| SHA | 标题 |
|---|---|
| `98d6b0d` | `fix(r28-v1): isolate live-PG legs in dual-backend store tests` |
| `df5522c` | `fix(r28-v2): ASCII-encode S3 object metadata title for non-ASCII docs` |

## 推送结果

```
git push origin HEAD:master
5a2a5b1..df5522c  HEAD -> master   (fast-forward, 无 SSL 错误)
```

`origin/master` 与本地 `master` 均已更新到 `df5522c`。

## 部署就绪度结论

**就绪（GO）**。理由：

1. **真实中间件全量回归 0 失败**：1530 passed / 6 skipped（skip 全为已知遗留），相比 R27 基线 +8 passed / −8 skipped，新增的 8 个全是此前被 skip 的 live-PG 证明测试真正跑通。
2. **M1 业务链路在真实 MinIO 下端到端绿**：上传 → 解析 → 发布 → 查询 → 反馈全通，对象成功落 MinIO，confidence 0.998，citation 命中。
3. **R28 修掉 2 个真实缺陷**：
   - S3 元数据 ASCII 编码 bug（影响所有非 ASCII 标题文档的对象存储写入——生产环境中文标题文档会全部命中此 bug，对象存储路径形同虚设）。
   - live-PG 测试隔离 + 断言 bug（测试质量缺陷，非生产 bug，但一直掩盖了「live-PG 证明从未真正跑过」的事实）。
4. **R28 改动最小且向后兼容**：3 个文件、+43/−4 行；S3 元数据修复不影响 DB 中标题存储；测试隔离修复只在测试侧加 DELETE 清理，不触碰生产代码。
5. **lint 无新增违规**：R28 改动文件全部 clean；全仓 61 个 pre-existing ruff 错误属历史技术债，不阻塞部署。

**已知遗留（不阻塞）**：
- 全仓 61 个 pre-existing ruff 错误（跨 ~15 个无关文件，建议后续单开一轮 `chore: ruff --fix` 清理）。
- 6 个 skip 测试（R27 `_SyncAdapter` asyncio 线程 ×2、Windows WAL ×1、`POSTGRES_TEST_DSN` 未设 ×2、故意 skip 行为验证 ×1）。
- editable install 指向 `wf_d772724a-24d-1` worktree（直接跑脚本需手动设 `PYTHONPATH`；pytest 不受影响）。
