# R32-B — LangGraph 并行子图编排（2026-06-22）

目标：闭合 spec §三 §5.2「Agent Runtime — 支持并行子任务和结果汇总」剩余的子图缺口。

R29-B 接通了 LangGraph 节点体（LLM + MCP 真调用），R31-B 接通了 `MemorySaver` checkpointer + `resume()`。但 ToolPlan 仍是**线性**迭代 `template.tool_names`——逐个 `mcp_client.invoke_tool(name, args)`，没有并行、没有结果聚合。spec §5.2 明确要求「并行子任务和结果汇总」。R31 spec §186 也把「真子图 + 多 gate 编排」列为后续。R32-B 用 LangGraph `Send` API 把 ToolPlan 改成并行子图。

R32 基线：`master` HEAD = `fa0115d`（R31 收尾 + gap-analysis §7.5），全量 in-memory 模式测试 green（1628 passed）、ruff exit 0。

## 改动总览

| 文件 | 改动 |
| --- | --- |
| `services/agent-platform-api/src/ai_employee/agent_platform_api/langgraph_runtime.py` | ToolPlan → 并行子图（`Send` fan-out + `ToolExec` worker + `ToolAggregate` reducer）；`LANGGRAPH_SUBGRAPH` env 门控；线性 fallback 保留 |
| `tests/test_r32b_parallel_subgraph.py` | 7 个测试钉住并行子图契约（并行执行 / 结果聚合 / 失败隔离 / checkpointer 兼容 / 只读模板 / 线性 fallback） |
| `tests/test_r30b_template_tool_invocation.py` | `ticket_summary` 调用序断言改为 set-based（并行 fan-out 后顺序非确定） |
| `tests/test_r30b_e2e_five_templates.py` | 同上，只读模板 invoked 顺序断言改为 `sorted == sorted` |

## 设计

### 子图拓扑

```
TemplateLoaded → RunStarted → ToolPlan ──fanout──► ToolExec ×N ──► ToolAggregate ──► (Completed | END)
                          │                                              │
                          └─approval─► ApprovalRequired ──resume──► ApprovalApproved ──fanout──► ToolExec ×N ──► ToolAggregate ──► END
```

- **ToolPlan**（plan-only）：seed `tool_calls` 每个工具一个 `planned` entry；conditional edge 返回 `[Send("ToolExec", {...}) for name in tool_names]` 把每个工具 fan-out 成并行 worker。
- **ToolExec**（worker）：`mcp_client.invoke_tool(name, args)`，记录 `tool_call_log` row，返回 `{"tool_results": [entry]}`。失败隔离——异常变 `failed` entry + `error_code`，其它 worker 照常完成。
- **ToolAggregate**（reducer）：`tool_results` 字段用 `Annotated[list, operator.add]` 合并所有 worker 输出；按 `tool_name` 把 worker 结果 merge 回 `tool_calls` 的 planned entry（保持模板声明顺序），并把原始结果 surface 到 `run.output["tool_results"]`；`change_assessment` 额外蒸馏出 `risk_factors` / `risk_level`。

### 审批模板的并行执行

`change_assessment`（3 工具，approval-required）是规范 fixture：

1. 首次 `run()`：ToolPlan 标 3 工具 `planned` → route `approval` → 在 `interrupt_before=["ApprovalRequired"]` 暂停（`waiting_approval`），MCP 未调用。
2. `resume(decision="approved")`：`update_state` 注入决策 → `invoke(None)` 驱动 → ApprovalRequired → ApprovalApproved。ApprovalApproved 的 conditional edge 再次 fan-out `Send` 到 ToolExec ×3 → ToolAggregate → END（`approval_status=="approved"` 时 `_route_after_tool_aggregate` 返回 `"end"` 直达 END，避免回 ApprovalRequired 死循环，也避免 `Completed` 节点覆盖审批结果）。

### 状态字段

`_RunState` 新增 `tool_results: Annotated[list[dict[str, Any]], operator.add]`——`operator.add` reducer 让并行 worker 的返回值 merge 而非覆盖。`run()` 初始 state 带 `tool_results: []`。线性 fallback 路径不触碰此字段，pre-R32 output 契约不变。

### 门控：`LANGGRAPH_SUBGRAPH`

- `LANGGRAPH_SUBGRAPH=true`（默认）：走并行子图。
- `LANGGRAPH_SUBGRAPH=false`：线性 fallback——ToolPlan 保留 R29-B 逐工具顺序调用；approval-required 工具在审批后直接 `planned → completed`（不真调用，pre-R32 语义）；只读模板照常顺序执行。fallback 不 populate `tool_results`，output 契约与 pre-R32 一致。

## TDD

`tests/test_r32b_parallel_subgraph.py`（先红后绿）：

1. `test_tool_plan_executes_tools_in_parallel_subgraph`——`change_assessment` 3 工具各 0.15s 延迟，断言执行窗口重叠（`sorted_starts[1] < sorted_ends[0]`），顺序非确定但各调用一次。
2. `test_subgraph_aggregates_results_to_output`——`run.output["tool_results"]` 含 3 工具结果；`risk_factors` 非空。
3. `test_subgraph_failure_isolates_failed_tool`——`ticket.history.search` raise，断言该工具 `failed` + `error_code`，另两个 `completed`；`tool_call_log` row 带 `error_code`。
4. `test_subgraph_preserves_checkpointer_resume`——共享 `MemorySaver` 的两个 runtime，runtime_a 暂停、runtime_b resume，3 工具在 resume leg 并行执行。
5. `test_subgraph_runs_for_readonly_template`——`ticket_summary`（2 工具，只读）单次 invoke 并行执行。
6. `test_linear_fallback_preserves_pre_r32_behaviour`——`LANGGRAPH_SUBGRAPH=false`，approval-required 工具不调用（`mcp.calls == []`），标 `completed`。
7. `test_linear_fallback_readonly_still_invokes_tools`——fallback 下只读模板照常顺序调用。

## 兼容性

- **R31-B checkpointer resume**：并行 fan-out 在正常 graph step 内发生，`interrupt_before=["ApprovalRequired"]` 与 `resume()` 契约不变；`test_subgraph_preserves_checkpointer_resume` 钉住跨 runtime resume。
- **R30-B prompt+model 归因**：`ToolExec` worker 继承 run 的 `model_name` / `prompt_version`，写进 `tool_call_log` row；`ToolCallSummary` 经 `ToolAggregate` merge 后仍带归因字段。
- **R29-B 真节点体**：LLM 调用（RunStarted）与 MCP 调用（ToolExec）保持真调用，DI 表面（`llm_client` / `mcp_client` / `tool_call_log` / `checkpointer`）不变。
- **API 契约**：`AgentRunResponse` shape 不变；`output` 新增 `tool_results` 字段（dict，非 schema 强约束），只读 / 审批模板在子图路径下都会 populate。线性 fallback output 与 pre-R32 一致。
- **顺序契约变更**：并行 fan-out 后工具调用顺序非确定。两个 R30-B 测试的顺序断言改为 set-based（`sorted == sorted`）。`ToolAggregate` 仍按模板 `tool_names` 声明顺序 reorder `tool_calls`，所以 response 里的 `tool_calls` 顺序保持确定。

## 验证

- `tests/test_r32b_parallel_subgraph.py`：7 passed。
- 全量 in-memory：`pytest tests/ --ignore=tests/test_local_ci.py` → **1635 passed, 14 skipped**（基线 1628 + 7 新增）。
- `ruff check` 全绿。

## 已知遗留

- 多 gate 编排（spec §5.2 更深的条件边 / 长运行多中断点）仍未做——R32-B 只闭合「并行子任务 + 结果汇总」这一条；多 interrupt gate 留待 R33+。
- `tool_results` reducer 是 in-process `operator.add`；跨 replica 的并行 fan-out 汇总依赖 checkpointer 持久化（`MemorySaver` 单进程，生产需 `RedisSaver` / `PostgresSaver`）——与 R31-B 同一限制。
- `change_assessment` 的 `risk_factors` 蒸馏是规则启发式（criticality / tickets / answer 拼接），未接 LLM 二次归纳——后续可把 ToolAggregate 升级成 LLM 节点。
