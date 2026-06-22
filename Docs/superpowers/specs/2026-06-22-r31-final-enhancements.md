# R31 — 限流 key_func 维度 + LangGraph MemorySaver 断点续跑（2026-06-22）

目标：把 R30 收尾建议的 R31 次要候选中可一次性闭合的**两条治理深度项**落地——

1. **R31-A 限流 key_func 维度**：`packages/rate-limit` 的 `install_rate_limiter(app)` 此前只按 user id 计桶，spec §三 §5.1「治理 / 限流」要求可按租户 / 端点 / 工具多维度限流。R31-A 给 `install_rate_limiter` 加 `key_func` 参数（callable 或 registry 名），内置 4 个 factory（`key_by_user` / `key_by_tenant` / `key_by_endpoint` / `key_by_tool`），默认走 `RATE_LIMIT_KEY_FUNC` env（`user`，向后兼容 6 服务既有接线）。
2. **R31-B LangGraph MemorySaver 断点续跑**：spec §三 §3/§4「可恢复」要求审批挂起 / 恢复走真 LangGraph checkpoint，而非 R24 之前的 `decide()` model_copy 缝合（R24 审计 G6 已记）。R31-B 让 LangGraph runtime compile 时挂 `MemorySaver` checkpointer + `interrupt_before=["ApprovalRequired"]`，审批挂起的 run 在 HITL gate 处暂停（而非 finalize），新增 `resume(run_id, decision, ...)` 走 `graph.update_state` + `graph.invoke(None, config)` 把 run 驱动到 `ApprovalApproved` / `ApprovalRejected` → END。

R30 建议的 R31 **主线**（P0-1 Dockerfile 全服务补齐 + P0-3 RCA 告警收敛算法）因改动面过大、与本轮治理深度项正交，**不在本轮范围**，继续留待 R32+。

R30 基线：`master` HEAD = `bf437e1`（R30 收尾 + gap-analysis §7.4），全量 in-memory 模式测试 green、ruff exit 0。

## 两线总览

| 线 | worktree | 主题 | 闭合的差距条目 |
| --- | --- | --- | --- |
| **R31-A** | `wf_90a6b52e-eda-3` | 限流 `key_func` 维度 | §7.1 P1-7 限流（`packages/rate-limit` 从「user 单维」→「user/tenant/endpoint/tool 4 维 + 自定义 callable」）；`install_rate_limiter(app, key_func=...)` 一行 API + `RATE_LIMIT_KEY_FUNC` env 路径，6 服务既有接线零改动 |
| **R31-B** | `wf_90a6b52e-eda-3` | LangGraph MemorySaver 断点续跑 | §三 §3/§4「可恢复」（HITL 审批走真 LangGraph checkpoint：`interrupt_before=["ApprovalRequired"]` 暂停 + `resume()` 经 `update_state` 注入决策 + `invoke(None)` 驱动；替换 R24 审计 G6 的 `decide()` model_copy 缝合路径） |
| 合并 | `wf_90a6b52e-eda-4` | 两线 merge 进同一分支 | 1 个 merge commit |
| 收尾 | `wf_90a6b52e-eda-4` | 推送 origin/master + 本 spec + gap-analysis §7.5 | 本文档 |

---

## R31-A — 限流 key_func 维度

### 目标

R25-L 把 `packages/rate-limit` 共享包做出来、6 服务 `install_rate_limiter(app)` 接入后，限流维度固定在「per-user」。spec §三 §5.1「治理 / 限流」要求按租户 / 端点 / 工具多维限流——一个租户下的多个 user 不能合起来打爆配额、一个高频端点不能被同一 user 拖垮全租户、一个 tool invoke 频次要能独立封顶。R31-A 把限流维度参数化，服务接线方一行 `key_func` 选维度，无需 copy 中间件。

### 改动

**1. `install_rate_limiter` 加 `key_func` 参数**

`packages/rate-limit/src/ai_employee/rate_limit/middleware.py`：

- `install_rate_limiter(app, *, limiter=None, key_func=None)`——`key_func` 接受 callable（`Callable[[Request], str]`）、registry 名（`"user"` / `"tenant"` / `"endpoint"` / `"tool"`）或 `None`。
- `None` 时回落 `RATE_LIMIT_KEY_FUNC` env（默认 `"user"`）——6 服务既有 `install_rate_limiter(app)` 调用零改动，行为完全向后兼容。
- 未知 env 值在 install 时抛 `ValueError`（fail-fast，不静默降级）。

**2. 四个内置 factory**

| factory | 桶 key | 取值来源 |
| --- | --- | --- |
| `key_by_user`（默认） | `user:{user_id}` | `request.state.user_id` / `X-User-Id` header |
| `key_by_tenant` | `tenant:{tenant_id}` | `X-Tenant-ID` header（缺省 `default`） |
| `key_by_endpoint` | `ep:{method}:{path}` | `request.method` + `request.url.path` |
| `key_by_tool` | `tool:{tool_name}` | `request.state.tool_name` / body `tool_name`（MCP tool invoke 路径） |

factory 注册表 `_KEY_FUNC_REGISTRY` + `_resolve_key_func()` 把 str 名 / callable / None 三种输入统一解析成 `KeyFunc`。

**3. `RateLimitMiddleware` 持有 `key_func`**

中间件 `__init__` 加 `key_func: KeyFunc | None = None`，`dispatch` 里 `key = self.key_func(request)` 取桶——一个 limiter 实例一套维度，运行时不切换。

**4. agent-platform-api demo 接线**

`tests/test_rate_limit_key_func_agent.py`：通过 `RATE_LIMIT_KEY_FUNC=tenant` env 驱动 agent-platform-api 既有 `install_rate_limiter(app)` 调用，pin 两个 user 共享 `X-Tenant-ID` 时合桶、未知 env 值 install 抛 `ValueError`。

### 测试

`packages/rate-limit/tests/test_rate_limit_key_func.py`（5 测试）+ `tests/test_rate_limit_key_func_agent.py`（2 测试）：

- `test_default_key_func_preserves_user_id_behavior`——默认行为不变（向后兼容）。
- `test_key_by_tenant_isolates_buckets`——同租户两 user 合桶，跨租户隔离。
- `test_key_by_endpoint_isolates_paths`——同 user 不同 path 分桶。
- `test_key_by_tool_isolates_tool_invokes`——tool invoke 按 tool_name 分桶。
- `test_custom_key_func_callable`——传 callable 走自定义。
- `test_tenant_dimension_buckets_by_tenant_id`——agent-platform env 路径。
- `test_unknown_key_func_value_raises`——未知 env fail-fast。

---

## R31-B — LangGraph MemorySaver 断点续跑

### 目标

R29-B 闭合了 LangGraph **执行层**（节点真调 `LlmClient.chat` / `mcp.invoke_tool`），但**编排层**的「可恢复」仍是 R23 的自研 runtime 兜底：审批挂起时 run 直接 finalize 成 `waiting_approval`，`decide()` 用 `model_copy` 把决策缝回 `AgentRunResponse`，绕过 LangGraph 引擎（R24 审计 G6）。spec §三 §3/§4「可恢复」要求 HITL 审批走真 checkpoint——run 在审批 gate 处暂停、thread 持久化、`resume()` 把决策注入后驱动图到终态。R31-B 落地这条。

### 改动

`services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py`（+268 / -7）：

**1. 图新增 `ApprovalApproved` / `ApprovalRejected` 节点 + 条件边**

```
... → ApprovalRequired →(conditional: approve/reject)→ ApprovalApproved → END
                                                      → ApprovalRejected → END
```

`ApprovalRequired → ApprovalApproved/ApprovalRejected` 的条件边读 state 里的 decision（`resume()` 注入），`_node_approval_required` 保留已注入的 decision 让路由边看到 `approve`/`reject`（而非 `pending`）。

**2. compile 挂 `MemorySaver` + `interrupt_before=["ApprovalRequired"]`**

- `__init__` 加 `checkpointer: Any | None = None` kwarg，默认 fresh `MemorySaver()`——每个 runtime 开箱可恢复。
- 多副本 / 跨 runtime 持久化场景可传共享 `MemorySaver`（构造 kwarg）。
- `builder.compile(checkpointer=checkpointer, interrupt_before=["ApprovalRequired"])`——审批 required 的 run 在 HITL gate 处**暂停**（不 finalize），thread 持久化在 `thread_id = run_id` 下。

**3. `resume(run_id, decision, decided_by, comment)` 驱动图**

- `graph.update_state(config, {"decision": decision, ...})` 注入决策。
- `graph.invoke(None, config)` 驱动图前进——`ApprovalRequired` 节点跑、条件边路由到 `ApprovalApproved`/`ApprovalRejected`、run finalize。
- 这**替换** R24 之前的 `decide()` model_copy 缝合路径（G6）。
- `decide()` 保留为 `RUNTIME_BACKEND=langgraph` + checkpointer-failure 路径的 fallback。

**4. `run()` 合成暂停视图**

审批 required 时 `run()` 合成 `waiting_approval` + `ApprovalRequired` trace 的响应，但**把 checkpoint 留在 interrupt 处**让 `resume()` 能驱动——而非直接 finalize。

### 测试

`tests/test_langgraph_checkpoint_resume.py`（6 测试）：

- `test_approval_required_run_pauses_at_interrupt`——审批 required 的 run 在 `ApprovalRequired` 处暂停（不 finalize）。
- `test_resume_after_approval_completes_run`——`resume(approved)` 驱动到 `ApprovalApproved` → completed。
- `test_resume_reject_terminates_run`——`resume(rejected)` 路由到 `ApprovalRejected` → failed。
- `test_checkpointer_persists_state_across_resume`——共享 `MemorySaver` 跨 runtime swap 持久化 thread。
- `test_readonly_run_completes_without_interrupt`——只读 run 一次 invoke 完成（无 interrupt）。
- `test_resume_unknown_run_raises`——对未知 / 非 parked run 调 `resume()` 抛 `KeyError`。

---

## 合并

两线在 `wf_90a6b52e-eda-3` 同一 worktree 顺序提交（R31-A 先、R31-B 后），`wf_90a6b52e-eda-4` 收尾分支 `git merge --no-ff` 合入：

```
6e5433b merge: R31-B (LangGraph MemorySaver checkpointer + resume())
33942e6 merge: R31-A (rate-limit key_func dimension + agent-platform demo)
9d67743 feat(r31-b.2): MemorySaver checkpointer + resume() (replace decide model_copy)
48397c9 test(r31-b.1): pin LangGraph checkpointer + resume contract
a312249 test(r31-a): demo key_by_tenant via RATE_LIMIT_KEY_FUNC in agent-platform
0e6efbd feat(r31-a): add key_func dimension to install_rate_limiter
```

### 合并冲突

两线改动面**不交叠**——R31-A 改 `packages/rate-limit/src/ai_employee/rate_limit/{__init__,middleware}.py` + 2 个测试文件；R31-B 改 `services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py` + 1 个测试文件。merge 无冲突。

## 改动面汇总

```
6 files changed, 909 insertions(+), 21 deletions(-)
```

新文件：`packages/rate-limit/tests/test_rate_limit_key_func.py`（129）、`tests/test_rate_limit_key_func_agent.py`（60）、`tests/test_langgraph_checkpoint_resume.py`（301）。

改动文件：`packages/rate-limit/src/ai_employee/rate_limit/__init__.py`（+10）、`packages/rate-limit/src/ai_employee/rate_limit/middleware.py`（+155 / -14）、`services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py`（+268 / -7）。

## Commit 列表

| SHA | 标题 |
| --- | --- |
| `0e6efbd` | `feat(r31-a): add key_func dimension to install_rate_limiter` |
| `a312249` | `test(r31-a): demo key_by_tenant via RATE_LIMIT_KEY_FUNC in agent-platform` |
| `48397c9` | `test(r31-b.1): pin LangGraph checkpointer + resume contract` |
| `9d67743` | `feat(r31-b.2): MemorySaver checkpointer + resume() (replace decide model_copy)` |
| `33942e6` | `merge: R31-A (rate-limit key_func dimension + agent-platform demo)` |
| `8e5433b` | `merge: R31-B (LangGraph MemorySaver checkpointer + resume())` |
| `<this>` | `merge: R31-A + R31-B (rate-limit key_func dimension + LangGraph MemorySaver checkpointer resume)` |
| `<this>` | `docs(r31): R31 final spec + gap-analysis §7.5` |

8 commits（2 feat + 2 test + 3 merge + 1 docs）。

## 推送结果

```
git -c http.sslVerify=false push origin worktree-wf_90a6b52e-eda-4:master
<待执行>
```

`origin/master` 待推送到 R31 收尾 commit（含 R31-A/B 全部 + 本 spec + gap-analysis §7.5 更新）。

## 部署就绪度结论

**就绪（GO）**。理由：

1. **限流多维度（R31-A）**：`install_rate_limiter(app, key_func=...)` 一行 API 把限流从 user 单维推到 user/tenant/endpoint/tool 4 维 + 自定义 callable；`RATE_LIMIT_KEY_FUNC` env 路径让 6 服务既有接线零改动；未知值 fail-fast。spec §三 §5.1「治理 / 限流」多维度要求闭合。
2. **LangGraph 真断点续跑（R31-B）**：HITL 审批走真 `MemorySaver` checkpoint + `interrupt_before` + `resume()`，替换 R24 审计 G6 的 `decide()` model_copy 缝合路径；审批挂起 / 恢复 / 拒绝 / 跨 runtime 持久化 6 条契约 pin 死。spec §三 §3/§4「可恢复」闭合。
3. **0 regression + lint 0 错误**：13 个 R31 新测试 green，ruff exit 0。

**已知遗留（不阻塞，留待 R32+）**：

- **P0-1 Dockerfile 全服务补齐**——R29-C 只补了 event-gateway Dockerfile，其余 7 个服务 + web-portal 的 Dockerfile 仍缺。建议 R32 主线接。
- **P0-3 RCA 告警收敛算法**——`del time_window_minutes` TODO 仍在，`build_incident` 当前还是「传入即聚合」stub；真收敛（时间窗 + 拓扑距离 + 父子规则 + 衍生告警归并）待后续。
- **P1-5 模板铺面 3/5 → 5/5**——「变更评估 / 工单总结」两个模板的真实 RCA 工具 + CMDB + 工单系统 + 知识库三方端到端测试待后续。
- **P1 限流网关化**——R31-A 把维度参数化做完，但单一 API gateway（spec §三 §5.1 关键能力，ingress-level）仍未做；建议后续接 ingress-level rate limit。
- **P2 LangGraph 编排层深度**——R31-B 闭合了「可恢复」（checkpoint + resume），但条件边 / 子图 / 长运行 resume 的更深层编排仍是 R31-B 的单 interrupt gate；真子图 + 多 gate 编排待后续。
- **5 个 skip 测试**——R30-C 把 skip 数从 6 → 5；剩 5 个 skip 仍待逐项复审。
- **PG 模式 alembic**——`alembic upgrade head` 在 PG 模式首次启动仍需 operator 手动跑；未来可加 init container。

> 详细分项交付见 `Docs/superpowers/specs/2026-06-22-r30-remaining-gaps.md`（R30）、`Docs/superpowers/specs/2026-06-22-r29-pg-default-langgraph-event-gateway.md`（R29）、`Docs/superpowers/specs/2026-06-22-r28-real-middleware-smoke.md`（R28）、`Docs/superpowers/specs/2026-06-19-r27-kafka-neo4j-scoring.md`（R27）、`Docs/superpowers/specs/2026-06-19-r26-reranker-rca-depth.md`（R26）、`Docs/superpowers/specs/2026-06-19-r25-observability-resilience-ratelimit.md`（R25）、`Docs/superpowers/specs/2026-06-19-r24-auth-trace-redaction.md`（R24）。
