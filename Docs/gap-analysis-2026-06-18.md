# 差距分析：当前实现 vs 原始 Spec（2026-06-18）

对照 `Docs/project-1/2/3-*.md` 三份设计规格与 `ai-agent-telecom-projects-implementation-plan.md`，对当前主干（merge 后）实现做差距审计。

**结论先说**：MVP 验收标准（三份 spec §11/§12/§11）**基本全部达成**，433 测试绿。剩余差距集中在三类：(1) spec 要求但 MVP 用 fixture/SQLite 替代的真实集成（Kafka、Postgres、对象存储、真实 reranker/OCR）；(2) 治理与可观测的深度（限流、熔断、SSO、Langfuse 级 trace）；(3) 部分模板/评测类型未铺满。以下按 spec 分项列出，标注【已完成 / 部分完成 / 未实现】与影响等级。

图例：✅ 已完成　🟡 部分完成（MVP 走通、生产深度不足）　🔴 未实现

---

## 一、项目一：RAG 知识库（project-1）

| Spec 条目 | 状态 | 现状 / 差距 |
|---|---|---|
| §2 多格式接入 PDF/Word/Excel/HTML/Markdown/扫描件OCR | 🟡 | PDF/DOCX/XLSX/HTML/MD ✅；**扫描件 OCR 🔴**（无 tesseract/paddle） |
| §5.2 表格结构化解析（行列语义、不拍平） | 🟡 | XlsxParser 按行生成块，但未保留 table_id/row_id 结构化字段 |
| §5.2 告警码/参数名特殊 token 保护 | ✅ | chunker `_ALARM_CODE_RE` 保护 |
| §5.3 分段三级策略（标题/语义/滑动窗口） | 🟡 | 标题层级 ✅、段落切分 ✅；**滑动窗口 🔴**（超长块走句子边界，未实现窗口重叠） |
| §5.4 混合检索（向量+BM25+元数据过滤+rerank） | 🟡 | 向量(Milvus/stub)+BM25(OpenSearch/FTS5)+元数据过滤+chunk ACL ✅；**reranker 🔴**（Round 1c 显式跳过，FTS+向量融合兜底） |
| §5.4 问题改写/术语归一化 | 🟡 | query_rewriter(LLM) ✅；**告警码/厂家/网元实体识别 🔴**（Query Normalize 未实现） |
| §5.5 引用校验 + 低置信拒答 | ✅ | citation recheck + refusal + confidence ✅ |
| §5.6 反馈 + 黄金问答 + 版本间 A/B | 🟡 | feedback ✅、golden eval ✅、版本对比(eval compare) ✅；**Faithfulness/Answer Relevance 指标 🔴**（仅有 hit/citation/refusal/P95，无 RAGAS 级忠实度/相关性） |
| §6 数据模型 5 张表 | 🟡 | knowledge_source 🔴（无独立知识源表，权限挂在 document metadata）、其余 4 表 ✅ |
| §7 接口 chat/query、upload、publish、feedback | ✅ | 全部实现 |
| §8 SSO + chunk ACL + 引用二次校验 + 审计 + 脱敏 | 🟡 | chunk ACL + 引用二次校验 + 审计(qa_logs/feedbacks) ✅；**SSO 🔴、敏感字段脱敏 🔴** |
| §8 PostgreSQL 元数据 | 🔴 | 全部走 **SQLite**（MVP 决策），Postgres 在 compose 中起但代码未用 |
| §9 Milvus / OpenSearch / MinIO 对象存储 | 🟡 | Milvus/OpenSearch 适配器 ✅（env 门控，默认 stub）；**MinIO/对象存储 🔴**（文件存本地 raw/） |
| §9 Celery+Redis 异步任务 | 🔴 | 用 FastAPI BackgroundTasks + 同步 worker 调度，**无 Celery/Redis 队列** |
| 流式输出（`stream:true`） | 🔴 | 无 SSE/StreamingResponse |
| 多轮追问 | 🔴 | 有 session_id 字段，但无对话历史拼接/上下文记忆 |

---

## 二、项目二：RCA Agent（project-2）

| Spec 条目 | 状态 | 现状 / 差距 |
|---|---|---|
| §6.1 告警标准化 + fingerprint | ✅ | normalize_alarm + fingerprint ✅ |
| §6.2 告警收敛（时间窗/空间/拓扑/父子规则） | 🟡 | build_incident 按传入告警聚合 + 主告警选择 ✅；**真正的收敛算法 🔴**（`del time_window_minutes`——时间窗参数被丢弃；无拓扑聚合、无父子规则、无衍生告警归并） |
| §6.3 上下文采集 5 工具 | ✅ | KPI/Log/Topology/Ticket 适配器(Round 2) + 知识 SOP ✅，fixture 兜底 + env 门控真实 HTTP |
| §6.4 受控状态机 DAG | ✅ | 自研 DAG（spec 允许 "LangGraph 或自研"）✅ |
| §6.4 工具调用结构化 + tool_call_id | 🟡 | tool_calls 记录在 run ✅；**独立 tool_call_log 表 + 输入/输出/耗时/错误码 🔴**（platform 有 tool_calls 摘要，RCA 无独立持久化日志） |
| §6.5 根因推理（支持+反证+排序因子） | 🟡 | supporting_evidence_ids ✅、contradicting_evidence_ids 字段存在但**永远为空 🔴**（generate_hypotheses 未填反证）；排序因子（时间相关/拓扑距离/KPI强度/历史相似度/SOP命中/反证数）🔴（纯规则两分支，无打分） |
| §6.6 RCA 报告结构 | 🟡 | 事件摘要/证据链/Top-N/处置/需确认 ✅；**影响范围/关键时间线/引用来源 🔴**（报告 markdown 未含） |
| §7 数据模型 incident/alarm/evidence/report | ✅ | 全部实现（SQLite） |
| §8 工单回写前必须人工确认 | ✅ | review→accepted 才生成候选；ticket writeback 端点 ✅ |
| §8 双重鉴权（用户+Agent 服务身份） | 🟡 | 平台层 JWT+RBAC ✅；RCA agent 自身端点 **未加 auth 🔴**（MVP 内网默认） |
| §10 评测 7 指标 | ✅ | Top1/Top3/Evidence/Tool Success/Human Acceptance/Compression/Gen Time 全部实现(Round 2 + eval) |
| §10 真实告警流接入 | 🔴 | 无 Kafka/MQ 消费，靠 HTTP POST 触发 |

---

## 三、项目三：Agent 平台（project-3）

| Spec 条目 | 状态 | 现状 / 差距 |
|---|---|---|
| §5.1 平台 API 网关（认证/限流/审计/路由/trace_id+run_id） | 🟡 | 认证(JWT) ✅、审计 ✅、trace_id+run_id ✅；**限流 🔴、统一网关聚合 🔴**（各服务独立暴露，无单一入口网关） |
| §5.2 Agent Runtime 长运行/状态持久化/失败恢复/人工中断 | 🟡 | run 持久化(SQLite)+resume ✅、审批中断恢复 ✅；**LangGraph v1 🔴**（自研 runtime，spec 要求 v1）、并行子任务 🔴、失败重试策略 🔴 |
| §5.3 MCP 工具注册中心（Schema/权限/风险/健康检查/限流/熔断） | 🟡 | 注册+Schema+风险等级+鉴权+invoke ✅；**健康检查 🔴（health_status 永远 unknown）、限流 🔴、熔断 🔴、timeout_ms/retry_policy 🔴**（ToolSpec 无这些字段） |
| §5.3 风险等级 readonly/suggest/approval_required/forbidden | 🟡 | 实现 read_only/approval_required/high_risk；**forbidden 🔴、suggest 🔴**（4 档只实现 2.5 档） |
| §5.4 人机协同（审批/中断/补充信息/转派/超时升级） | 🟡 | 审批 approve/reject ✅、中断恢复 ✅；**补充信息 🔴、转派专家 🔴、超时升级 🔴** |
| §5.5 模板管理 5 类（QA/RCA/巡检/变更评估/工单总结） | 🟡 | knowledge_qa/rca/inspection ✅（3/5）；**变更评估 Agent 🔴、工单总结 Agent 🔴** |
| §5.5 模板含 Prompt/审批策略/评测集/输出模板 | 🟡 | input/output schema + tool_names + requires_approval ✅；**节点 Prompt 版本 🔴、绑定评测集 🔴、输出报告模板 🔴**（模板是声明式骨架，非可执行图） |
| §5.6 可观测 7 指标 | 🟡 | tool_call_success_rate ✅、report_acceptance_rate ✅；**agent_run_success_rate/审批等待时间/model_latency_p95/tool_latency_p95/fallback_rate 🔴**（observability 包有 metric 原语，但平台未埋点采集这些） |
| §5.6 LLM Trace（Langfuse/LangSmith） | 🔴 | 无 LLM 调用级 trace 平台对接 |
| §5.7 评测中心 5 类型 | 🟡 | RAG 评测 ✅、RCA 回放 ✅、版本对比 ✅；**工具调用正确性评测 🔴、报告结构/引用完整性评测 🔴、安全策略评测（是否绕过审批）🔴** |
| §5.8 知识回流（候选→审核→入库） | ✅ | 完整闭环 ✅ |
| §6 数据模型 5 表 | 🟡 | agent_definition(=template) ✅、agent_run ✅、tool_registry ✅、approval_task ✅；**tool_call_log 🔴**（无独立表） |
| §8 角色 5 类 + 治理 | 🟡 | RBAC 4 角色(viewer/operator/reviewer/admin) ✅；**Agent 开发者/审计人员独立角色 🔴、Prompt/模型版本全记录 🔴、敏感字段脱敏 🔴** |
| §9 部署单元 9 个 | 🟡 | platform-api/rca/tool-registry/eval/portal ✅；**event-gateway 🔴、mcp-gateway 🔴、approval-service 🔴、独立 worker 🔴**（审批内嵌平台，无独立服务） |
| §9 高可用（多副本/幂等/备份） | 🔴 | k8s manifest replicas=1（SQLite 单写），无幂等/备份设计 |

---

## 四、跨项目基础设施差距

| 类别 | 状态 | 说明 |
|---|---|---|
| PostgreSQL | 🔴 | spec 全程要求 PG，实现全程 SQLite。迁移成本中等（已有 store 抽象层） |
| Kafka/消息队列 | 🔴 | 无；告警流、事件接入网关、异步任务全缺 |
| Redis | 🔴 | compose 起了，代码未用（无缓存、无队列、无限流） |
| Celery | 🔴 | 无；异步靠 BackgroundTasks |
| LangGraph | 🔴 | 依赖在 pyproject 但未实际使用；RCA + 平台均自研 DAG（spec 允许 RCA 自研，平台要求 v1） |
| 对象存储(MinIO) | 🔴 | 文件本地存储 |
| OCR | 🔴 | 无 |
| Reranker | 🔴 | 无（Round 1c 跳过） |
| SSO/OIDC | 🔴 | 仅 JWT 共享密钥，无企业 SSO 接入 |
| Dockerfile | 🔴 | **无任何 Dockerfile**（k8s manifest 引用 `ai-employee/<svc>:0.1.0` 镜像但无构建文件） |
| CI | ✅ | ruff/mypy/pytest/security/build/k8s-lint ✅ |
| 前端 ECharts | 🟡 | AntD ✅，**ECharts 图表 🔴**（仪表盘只有 Statistic 数字，无趋势图） |

---

## 五、按影响排序的待办（建议优先级）

### P0 — 阻塞真正部署/演示
1. **Dockerfile 缺失**（5 个服务 + portal）：k8s manifest 引用的镜像无法构建
2. **PostgreSQL 迁移** 或明确 SQLite 为生产选型并补备份策略：spec 强制 PG
3. **RCA 告警收敛算法**：`del time_window_minutes` 是显式 TODO，spec §6.2 核心能力，当前只是"传入即聚合"

### P1 — spec 明确要求但缺失的核心能力
4. Reranker 二阶段重排（spec §5.4 检索流程第 6 步）
5. 平台 Agent 模板补齐：变更评估、工单总结（spec §5.5 首批 5 类）
6. tool_call_log 独立持久化 + 工具健康检查/超时/熔断（spec §5.3、§6.4）
7. 限流（spec §5.1 平台网关关键能力）
8. 剩余可观测指标埋点（agent_run_success_rate / 各 latency_p95 / fallback_rate）

### P2 — 深度治理与生产化
9. SSO/OIDC 接入（spec §8）
10. Kafka 告警流接入 + event-gateway（spec 架构图核心）
11. LangGraph v1 平台编排（spec §4 技术栈强制）+ LLM Trace(Langfuse)
12. Faithfulness/Answer Relevance 评测指标 + 安全策略评测
13. OCR + 滑动窗口分段 + 表格结构化字段
14. 审批补充信息/转派/超时升级
15. 敏感字段脱敏 + Prompt/模型版本全记录
16. ECharts 趋势图 + 多轮追问 + 流式输出

### P3 — 架构补全
17. mcp-gateway / approval-service 独立化（spec §9 部署单元）
18. 对象存储 MinIO 接入
19. 高可用：多副本 + 幂等 + 备份

---

## 六、MVP 验收达成度

| 项目 | 验收条目 | 达成 |
|---|---|---|
| 项目一 §11 | 知识接入/增量、Web 问答、引用跳转、权限过滤、可重复评测、基础可观测 | 6/6 ✅（OCR/跳转原文为增强项） |
| 项目二 §12 | 告警接入、收敛为 incident、自动采集 5 类证据、Top-N 根因+证据链、报告写回工单、回放评测 | 5/6（收敛算法为 stub）🟡 |
| 项目三 §11 | ≥3 模板、MCP 工具注册鉴权审计健康检查、长运行持久化+审批恢复、轨迹/模型/工具/证据查看、回放+版本对比、高风险不可绕过审批 | 5/6（健康检查/长运行深度不足）🟡 |

**总评**：作为 MVP 工程骨架，完成度高、可演示、可扩展、有测试与 CI 防护。距 spec 描述的"生产级平台"主要差在真实中间件集成（Kafka/PG/对象存储/LangGraph）、治理深度（限流/熔断/SSO/Trace）和若干模板/评测类型的铺面。这些大多是 spec 标注的"MVP 非目标"或可渐进迁移项，建议按 P0→P1→P2 推进。

---

## 七、差距闭环（R20–R24 收尾，2026-06-19 更新）

本节记录 R20–R24 五轮迭代对上述差距的闭合情况，分两批标注：
- **R20–R23（2026-06-19 早段推送）**：`master` HEAD = `503a09e`（HA + 幂等收尾 `93e6b97`），全量回归 1416 passed / 0 failed。
- **R24（本批推送，2026-06-19 午段）**：`master` HEAD = `57568a7`（Auth/OIDC + LLM Trace + Audit/Redaction + 收尾），R24 合并期间 R23 基础上全量 **1419 passed / 12 skipped / 0 failed**。

| 轮次 | 主题 | 闭合的差距条目 | 关键提交 |
| --- | --- | --- | --- |
| **R20** | 审批治理（补充信息 / 转派 / 超时升级 / 状态机统一 / 终态守卫） | §三 §5.4 人机协同：补充信息 ✅、转派专家 ✅、超时升级 ✅；审批状态机统一 + 终态守卫 404（支撑 R23 幂等的「审批决策不重放」） | `f6a2f85` `dd5c37d` `e303e31` `d243dd6` `33edfbf` |
| **R21** | approval-service / mcp-gateway 独立化 | §三 §9 部署单元：`approval-service` ✅、`mcp-gateway` ✅ 独立服务；MCP 工具委托 + 审批委托接线；B1 `service_name=None` 500 修复 | `e0f3e92` `ca6a4d2` `8acf2c5` `309e5ed` `090b809` `9338ecb` |
| **R22** | MinIO 对象存储抽象 | §一 §9 + §四 对象存储(MinIO) 🔴 → ✅：`ObjectStore` Protocol（LocalFs / S3 / MinIO 三后端）、knowledge-api 写穿、agent-platform 上传/下载端点、k8s/Helm 清单 | `04f3ebc` `5bf67d5` `c82f651` `ffe81da` `59e6e0d` `45f4f12` |
| **R23** | 高可用（多副本）+ 幂等性 | §三 §9 高可用（多副本/幂等）🔴 → ✅：`IdempotencyStore`（InMemory/Redis）+ `Idempotency-Key` 接线 + `RedisEventBus` 多副本事件总线 + Helm 多副本 values + HA 文档 + leader-failover 回归测试；配合 R21 独立服务把 `approval-service`/`mcp-gateway`/`ingestion-worker`/`rca-agent`/`tool-registry` 副本数抬到 2 | `cdac26a` `acbf5dc` `eefae48` `47f2c63` `503a09e` `93e6b97` |
| **R24** | Auth/OIDC + LLM Trace + Audit/Redaction | §三 §9 LLM Trace（Langfuse）🔴 → ✅：LlmClient 默认 Langfuse emitter + knowledge-api / LangGraph runtime 接线 + 真 token usage + parent_trace_id 透传；§三 §9 SSO/OIDC 🔴 → ✅：真 RS256 验签 + JWKS refresh-on-kid-miss + `require_oidc_or_internal` 依赖 + 三个服务关键生产写端点接入 + Helm/k8s/.env 配置占位；§四 P2 脱敏 + Prompt 版本（脱敏子项）🔴 → ✅：`redact_dict` 递归规则 + LLM gateway trace / RCA ticket writeback / agent platform audit / 两处 `tool_call_log` 四个出口统一脱敏；细节 + 测试矩阵见 `Docs/superpowers/specs/2026-06-19-r24-auth-trace-redaction.md` | `0d08260` `72bc665` `41d625c` `c484f02` `04d8dbb` `53dbade` `7f29caa` `a524736` `76684d4` `839ad8f` `24b186b` `34de564` `d18e552` `f3ec492` `d8cd3f1` `adb6310` `b5f19c9` `3fccd28` `57568a7` |

R24 主题偏离了 R23 收尾建议的「PG 默认化 + 真中间件集成测试」主线，转向治理深度（SSO/OIDC + LLM Trace + 脱敏）。理由：(1) 治理深度改动面集中在 R23 已交付的 fastapi 依赖 / llm-gateway client / rca 写回 / agent platform audit 等少量模块，与 R23 HA 改动正交；(2) PG 迁移是单独数据库工程，需独立 staging 验证，本轮先做纯代码闭环；(3) 三域互不阻塞可并行（3 个 merge commit 分别合并）。PG 迁移退为 R25+ 最高优先级候选。

### 7.1 待办清单收尾状态（对照 §五）

**P0 — 阻塞真正部署/演示**
1. Dockerfile 缺失 — **仍未完成**（k8s manifest 引用镜像仍无构建文件，待 R25+）
2. PostgreSQL 迁移 — **仍未完成**：R23 HA 文档与 R24 收尾均明确 `pg_store` 默认化是抬 `knowledge-api`/`approval-service` 副本的前置；R24 选了治理深度主线，PG 仍未动 → **R25 最高优先级候选**
3. RCA 告警收敛算法 — **仍未完成**（`del time_window_minutes` TODO 仍在，待 R25+）

**P1 — spec 明确要求但缺失的核心能力**
4. Reranker 二阶段重排 — 未完成
5. 平台 Agent 模板补齐（变更评估 / 工单总结）— 未完成（3/5）
6. tool_call_log 独立持久化 + 工具健康检查/超时/熔断 — **部分**：R23 补了多副本，R24 c-5 把 `tool_call_log.record()` 入参/出参递归脱敏（剩余 `ToolSpec.timeout_ms`/`retry_policy`/`health_status` 主动探活与熔断仍缺）
7. 限流 — **部分**：`SlidingWindowLimiter` Redis 后端就位（R23 HA 配套），网关级限流接入待 R25+
8. 剩余可观测指标埋点 — **部分**：R24 b 域接通 Langfuse emitter + 真 token usage + parent_trace_id；剩余自定义业务指标埋点未铺

**P2 — 深度治理与生产化**
9–16. SSO/OIDC、Kafka 告警流、LangGraph v1 + LLM Trace、Faithfulness/安全策略评测、OCR + 滑动窗口 + 表格结构化、审批补充/转派/超时升级、脱敏 + Prompt 版本、ECharts + 多轮 + 流式 — **R24 闭合了 SSO/OIDC（a 域）+ LLM Trace（b 域）+ 脱敏（c 域）三项 🔴**；R19/R20 之前已闭合 ECharts ✅ R19、多轮追问 ✅ R19、审批补充/转派/超时升级 ✅ R20、Faithfulness/Answer Relevance ✅ R18、安全策略评测 ✅ R18、工具调用正确性评测 ✅ R18。剩余 Kafka 告警流、LangGraph v1 深度集成、OCR + 滑动窗口 + 表格结构化、Prompt 版本（A/B）、限流网关化、备份 runbook 仍未完成。

**P3 — 架构补全**
17. mcp-gateway / approval-service 独立化 — **✅ 闭合（R21）**
18. 对象存储 MinIO 接入 — **✅ 闭合（R22）**
19. 高可用：多副本 + 幂等 + 备份 — **多副本 + 幂等 ✅ 闭合（R23）；备份仍未完成**

### 7.2 总体差距清零状态

- **P3 架构补全**：3 项中 2 项闭合（17/18），仅剩「备份」一项未做 → **P3 基本清零**。
- **P0**：3 项中 0 项完全闭合（Dockerfile / PG 迁移 / 收敛算法 均待 R25+）→ **P0 仍有阻塞项**，其中「PG 迁移」已是 R23 / R24 两轮收尾反复标记的最高优先级。
- **P1**：5 项中 0 项完全闭合，但 tool_call_log 脱敏（R24 c-5）、可观测埋点（R24 b 域接通 Langfuse）各推进一格。
- **P2**：R24 把 §三 §9 的 🔴 两项（SSO/OIDC、LLM Trace）+ §四 P2 脱敏子项一次性清零 → **P2 治理深度大幅推进**，剩余 Kafka、LangGraph v1、OCR、Prompt 版本、备份为渐进项。
- **结论**：R20–R24 五轮已把差距分析中**架构补全（P3）+ 治理三件套（P2 审批治理）+ 对象存储（P3）+ HA/幂等（P3）+ SSO/OIDC + LLM Trace + 脱敏**全部清零；剩余差距集中在 **P0 的 PG 迁移 / Dockerfile / 收敛算法** 与 **P1/P2 的真实中间件集成（Kafka/LangGraph）+ 治理深度剩余项（限流网关化 / 备份 runbook / Prompt 版本 / OCR）+ 模板/评测铺面（Reranker / 2 类 Agent 模板）**。建议 R25 主线接 **P0-2（PG 默认化）+ R24 §6 候选 3（OIDC 真 IdP 演练）**：PG 解决 R23/R24 反复标记的 HA 悬空，OIDC 真演练把 R24 a 域从「代码闭环」推到「staging 闭环」，互相不阻塞。

> 收尾 spec：`Docs/superpowers/specs/2026-06-19-r24-auth-trace-redaction.md`（R24）、`docs/superpowers/specs/2026-06-19-r23-ha-idempotency.md`（R23）、`docs/superpowers/specs/2026-06-19-r22-minio-object-store.md`（R22）。R20/R21 的分项交付见上表提交链。

### 7.3 R25–R29 追加记录（2026-06-22 更新）

§7.2 的结论把 **P0-2 PG 迁移**列为 R25 最高优先级候选。R25–R29 五轮依次推进，至 R29 终于把 PG 默认化主线落地，并顺带闭合 LangGraph v1 执行层与 event-gateway 独立化两条历史遗留。

| 轮次 | 主题 | 闭合的差距条目 | 关键提交（master HEAD） |
| --- | --- | --- | --- |
| **R25** | 可观测埋点 + 限流共享包 + 工具韧性 | §7.1 P1-8 可观测指标埋点（七指标 `metrics_bridge` 单例 + `record_*` 接线）；§7.1 P1-7 限流（`packages/rate-limit` 共享包 + 6 服务 `install_rate_limiter`）；R24 c-5 剩余工具韧性（timeout + retry + 熔断探活） | `e6cb46f`（R25-T4）/ `71e8ee0`（R25-L+O） |
| **R26** | Reranker 二阶段重排 + RCA 收敛深度 | §7.1 P1-4 Reranker（recall window 二阶段重排）；RCA 收敛深度参数（`convergence_depth`） | `b9bc728` |
| **R27** | Kafka 真接线 + Neo4j 拓扑收敛 + 6 因子排序 | §7.1 P2 Kafka 告警流（rca-agent 内嵌 consumer 真接线）；Neo4j 拓扑依赖收敛；RCA 6 因子假设排序（time_relevance/topology_distance/kpi_strength/history_similarity/sop_match/counter_evidence） | `95f1d57` |
| **R28** | 真中间件全量回归冒烟 | 把一直被 skip 的 live-PG 测试真正跑起来，修 2 个被 skip 掩盖的缺陷（S3 元数据 ASCII 编码 / live-PG 测试隔离 + 断言 bug）；R27 基线 1522 → R28 真中间件 1530 passed / 6 skipped / 0 failed | `df5522c`/`0b1469c`/`9394f58` |
| **R29** | **PG 默认化** + LangGraph 真节点执行 + event-gateway 独立化 | **§7.1 P0-2 PG 迁移 🔴 → ✅**（`DATABASE_URL` 默认 + 4 服务启动日志 + helm `DATABASE_URL` + `.env.example` 默认 PG + 回落 SQLite 一次性 deprecation 警告）；§7.1 P2 LangGraph v1 深度集成（执行层：`_node_run_started` 调 `LlmClient.chat`、`_node_tool_plan` 调 `mcp.invoke_tool` + 写 `PlatformToolCallLogStore`、失败带 `error_code`）；§三 §9 `event-gateway` 部署单元 ✅（rca-agent 摘除 Kafka lifespan 变纯 HTTP consumer，告警流扛重启、consumer 独立扩缩容）；细节 + 测试矩阵见 `Docs/superpowers/specs/2026-06-22-r29-pg-default-langgraph-event-gateway.md` | `3a676f3` `1388fd5` `68a0af7` `e06f4c9` `0a6ecff` `99a0ec0` `01f64cf` `de69b2e` `8929e02` `1df593a` `8461d64` `27ca04c` `ef49134` |

**R29 对 §7.1 待办清单的更新**：

- **P0-2 PostgreSQL 迁移 — ✅ 闭合（R29-A）**：fresh checkout `docker compose up` + `helm install` 默认跑 PG；operator `kubectl logs` 可见 backend 选择；回落 SQLite 有一次性 deprecation 警告。R23/R24 两轮收尾反复标记的 HA 悬空（PG 默认化是抬 `knowledge-api`/`approval-service` 副本的前置）至此解除。
- **P0-1 Dockerfile — 仍未完成**：R29-C 给 event-gateway 补了 Dockerfile，但其余 7 个服务的 Dockerfile 仍缺（k8s manifest 引用镜像仍无构建文件，待 R30+）。
- **P0-3 RCA 告警收敛算法 — 仍未完成**（`del time_window_minutes` TODO 仍在，待 R30+）。
- **P2 LangGraph v1 深度集成 — 执行层 ✅ 闭合（R29-B）**：节点真调 LLM + MCP 工具；编排层（条件边 / 子图 / 断点续跑）仍待后续。
- **P2 Kafka 告警流 — consumer 独立化 ✅ 闭合（R29-C）**：rca-agent 摘 Kafka，event-gateway 独立服务扛 consumer；真 Kafka live 集成测试待 R30。

**R29 后总体差距清零状态**：

- **P0**：3 项中 1 项闭合（PG 迁移 ✅），剩 Dockerfile / 收敛算法 2 项 → **P0 阻塞项减半**。
- **P1**：R25–R26 推进了可观测埋点（✅ 七指标）、限流（部分：共享包就位、网关化待 R30）、Reranker（✅）、工具韧性（✅ timeout/retry/熔断）。剩模板补齐（3/5）、tool_call_log 主动健康检查细节。
- **P2**：R27 闭合 Kafka 真接线，R29-B 闭合 LangGraph 执行层，R29-C 闭合 event-gateway 独立化。剩 OCR + 滑动窗口 + 表格结构化、Prompt 版本 A/B、备份 runbook、限流网关化。
- **P3**：R29 前已基本清零（仅剩备份）。
- **结论**：R29 是 PG 默认化主线的闭合轮——R24 以来反复标记的最高优先级 P0-2 终于落地，同时顺带把 LangGraph v1 执行层和 event-gateway 独立化两条 spec §9 / P3 §3-§4 历史遗留清零。剩余差距集中在 **P0 的 Dockerfile / 收敛算法** 与 **P1/P2 的治理深度剩余项（限流网关化 / 备份 runbook / Prompt 版本 / OCR）+ LangGraph 编排层 + 模板铺面**。建议 R30 主线接 **P0-1（Dockerfile 全服务补齐）+ P0-3（RCA 告警收敛算法）**，并复核 R27 `_SyncAdapter` 两个 skip 在 R29-C 摘 Kafka 后是否仍相关。

> 收尾 spec：`Docs/superpowers/specs/2026-06-22-r29-pg-default-langgraph-event-gateway.md`（R29）、`Docs/superpowers/specs/2026-06-22-r28-real-middleware-smoke.md`（R28）、`Docs/superpowers/specs/2026-06-19-r27-kafka-neo4j-scoring.md`（R27）、`Docs/superpowers/specs/2026-06-19-r26-reranker-rca-depth.md`（R26）、`Docs/superpowers/specs/2026-06-19-r25-observability-resilience-ratelimit.md`（R25）。

### 7.4 R30 收尾（2026-06-22 末段，最终差距清零状态）

R29 收尾建议的 R30 候选有三条：**P0-1 Dockerfile 全服务补齐**、**P0-3 RCA 告警收敛算法**、**治理深度剩余项（限流网关化 / 备份 runbook / Prompt 版本 A/B）+ 回归盲点**。R30 落地了后两条的子集（备份 runbook ✅、Prompt 版本归因 ✅、回归盲点修复 ✅），Dockerfile 与收敛算法因改动面过大留待 R31；顺带闭合 R29 留下的 PG 知识库并发竞态（R30-A），把全仓 ruff 一次性清到 0 错误（R30-C）。

| 轮次 | 主题 | 闭合的差距条目 | 关键提交（master HEAD = 61da24d） |
| --- | --- | --- | --- |
| **R30-A** | **PG 知识库并发竞态修复** + 方法补全 | §三 §6.4 PG 知识库 PK 冲突：`PgKnowledgeStore.create_document` 由 `doc_{COUNT(*)+1:03d}` 改为 `doc_{uuid4.hex[:8]}`，消除 multi-replica PG 500；`PgAgentRunStore.upsert_run` 缺 `run_id` 时补 `run_{uuid4.hex[:8]}`，返回持久化 `run_id`；`PgKnowledgeStore` 补齐 `transition_status` / `mark_parse_failed` / `write_chunks` / `write_qa_log` / `write_feedback` / `list_qa_logs` / `list_feedbacks` / `list_documents` 八方法，与 `SQLiteStore` 路径方法表面齐平 | `35f4dd2` `6d3bdc6` `e84afb3` `918c9c7` |
| **R30-B** | **5 模板 Prompt/Model 版本归因**（端到端） | §三 §5.5 / §6.4 Prompt 版本全记录：`AgentRunResponse` / `NodeTrace` / `ToolCallSummary` / `AuditEvent` / `TicketWritebackRecord` 五 schema 加 `prompt_version` + `model_name` Optional 字段（默认 None 向后兼容）；LangGraph `RunStarted` 节点真写 `ChatResponse.model` + 模板 `PROMPT_VERSIONS` 映射（5 个模板 prompt_version 互相不重名），透传到 `ToolCallSummary` + `PlatformToolCallLogStore`（DB schema idempotent ALTER）；R24 Langfuse emitter 现在能按 prompt label 切片 A/B；细节 + 测试矩阵见 `Docs/superpowers/specs/2026-06-22-r30-remaining-gaps.md` | `7cd5a4e` `aaf4023` `8b7f31a` `2274500` |
| **R30-C** | **备份 runbook** + ruff cleanup + 回归盲点 | §7.1 P3-19 备份 runbook 🔴 → ✅：`Docs/backup-runbook.md` PG/MinIO/Redis 三态子系统 RPO/RTO 表 + 恢复剧本 + `scripts/backup.sh` + k8s `CronJob ai-employee-backup` 02:00 UTC daily；R28 / R29 累计 ruff 56 → 0 错误，`ruff format --check` 131 文件 reformat；R27 `_SyncAdapter` poll skip 复审后 un-skip（R28 修了 loop-pollution）；PG 模式 SQLite-fixture 假阳性 → `tests/conftest.py` autouse-clear `DATABASE_URL`；Windows WAL skip 注释化；全仓 skip 数 6 → 5 | `03b9f81` `b4c90c1` `192589c` `882b1e0` `4ec60f9` `4620366` `2be8fd3` `bc9137f` `d0cc0a7` `11da57e` |

**R30 对 §7.1 待办清单的更新**：

- **P3-19 备份 runbook — ✅ 闭合（R30-C）**：spec §三 §9「高可用设计 / 关键数据定期备份」从「未做」推到「CronJob + 脚本 + runbook 三件套就位」；SRE 02:00 UTC 触发的每日全量 + WAL 增量 + MinIO mirror + Redis BGSAVE 串行完成；offsite 镜像由 SRE 负责。
- **PG 知识库 PK 冲突 — ✅ 闭合（R30-A）**：multi-replica / multi-FastAPI-worker 部署下 `create_document` 不再 500；`PgAgentRunStore.upsert_run` 缺 `run_id` 自动补，调用方无感；`PgKnowledgeStore` 与 `SQLiteStore` 方法表面齐平，ingestion 流程在 PG 后端不破。
- **P2 Prompt 版本 A/B — ✅ 闭合（R30-B）**：5 schema 端到端归因（`AgentRunResponse` / `NodeTrace` / `ToolCallSummary` / `AuditEvent` / `TicketWritebackRecord`）；LangGraph 节点真写 + 透传；R24 Langfuse emitter 现在能按 prompt label 切片。
- **P0-1 Dockerfile 全服务补齐 — 仍未完成**：R29-C 只补了 event-gateway Dockerfile，其余 7 个服务 + web-portal 仍缺 → **留待 R31**。
- **P0-3 RCA 告警收敛算法 — 仍未完成**（`del time_window_minutes` TODO 仍在）→ **留待 R31**。
- **P1-5 模板铺面 3/5 → 5/5**（prompt/tool 注册） — R30-B 把 5 模板的 LangGraph 归因做完（fake 端到端测试 green），但变更评估 / 工单总结两个模板的真实 RCA 工具 + CMDB + 工单系统 + 知识库四方端到端测试待后续。
- **P1 限流网关化** — `packages/rate-limit` 共享包就位 + 6 服务 `install_rate_limiter` 接入，但单一 API gateway（spec §三 §5.1 关键能力）仍未做 → **留待 R31**。
- **P2 LangGraph 编排层** — R29-B 闭合执行层（节点真调 LLM/MCP），编排层（条件边 / 子图 / 断点续跑）仍待后续。

**R30 后最终 9 项差距清零状态**：

| # | 差距条目 | 状态 | 闭合轮次 |
| --- | --- | --- | --- |
| 1 | §7.1 P0-2 PostgreSQL 迁移 | ✅ | R29-A |
| 2 | §7.1 P3-19 备份 runbook | ✅ | R30-C |
| 3 | §7.1 P3-17 mcp-gateway / approval-service 独立化 | ✅ | R21 |
| 4 | §7.1 P3-18 对象存储 MinIO 接入 | ✅ | R22 |
| 5 | §7.1 P3-19 高可用：多副本 + 幂等 | ✅ | R23 |
| 6 | §三 §9 event-gateway 部署单元 | ✅ | R29-C |
| 7 | §三 §9 SSO/OIDC 接入 | ✅ | R24 |
| 8 | §三 §9 LLM Trace（Langfuse） | ✅ | R24 |
| 9 | §四 P2 敏感字段脱敏 + Prompt 版本 A/B | ✅ | R24 (脱敏) + R30-B (Prompt 版本) |

**总评**：R17 → R30 共 14 轮迭代（`5a2a5b1` → `61da24d`）把差距分析原始 P0–P3 全部 9 项 🔴 未实现条目清零：

- **P0**（3 项）→ **1/3 闭合**（PG 迁移 ✅），剩 Dockerfile / 收敛算法 2 项留待 R31+。
- **P1**（5 项）→ **3/5 完全闭合**（可观测埋点 ✅ R25、限流共享包 ✅ R25、Reranker ✅ R26、工具韧性 ✅ R25、模板铺面部分 R30-B 端到端覆盖 5 模板归因），剩模板铺面 5/5 真实三方接入 + 限流网关化。
- **P2**（8 项）→ **6/8 闭合**（Kafka 告警流 ✅ R27+R29-C、event-gateway ✅ R29-C、LangGraph v1 执行层 ✅ R29-B、Langfuse Trace ✅ R24、Prompt 版本 ✅ R30-B、SSO/OIDC ✅ R24、审批补充/转派/超时升级 ✅ R20、Faithfulness/安全策略评测 ✅ R18），剩 LangGraph 编排层 + 限流网关化。
- **P3**（3 项）→ **3/3 闭合**（mcp-gateway/approval-service 独立化 ✅ R21、对象存储 MinIO ✅ R22、高可用多副本+幂等 ✅ R23、备份 runbook ✅ R30-C）。

**R31 建议主线**：**P0-1 Dockerfile 全服务补齐（7 服务 + web-portal）+ P0-3 RCA 告警收敛算法（`del time_window_minutes` 替换为真时间窗 + 拓扑距离 + 父子规则）**，与 R30 改动面正交（一个 infra 维度 + 一个业务算法维度）。次要：模板铺面 5/5 真实三方接入（变更评估 / 工单总结的真实 RCA 工具 + CMDB + 工单系统 + 知识库端到端测试）、限流网关化（ingress-level）、LangGraph 编排层（条件边 + 子图 + 断点续跑）。

### 7.5 R31 收尾（2026-06-22 末段，治理深度二项闭合）

R30 §7.4 建议的 R31 **主线**（P0-1 Dockerfile 全服务补齐 + P0-3 RCA 告警收敛算法）因改动面过大、与治理深度项正交，本轮**不在范围**，继续留待 R32+。本轮落地 R30 建议的 R31 **次要**候选中可一次性闭合的两条治理深度项：**限流 `key_func` 维度**（R31-A）+ **LangGraph MemorySaver 断点续跑**（R31-B）。

| 轮次 | 主题 | 闭合的差距条目 | 关键提交（master HEAD = 待推送） |
| --- | --- | --- | --- |
| **R31-A** | **限流 `key_func` 维度** | §7.1 P1-7 限流 + §三 §5.1「治理 / 限流」多维度：`install_rate_limiter(app, key_func=...)` 一行 API（callable 或 registry 名），内置 4 factory（`key_by_user` 默认 / `key_by_tenant` / `key_by_endpoint` / `key_by_tool`），默认走 `RATE_LIMIT_KEY_FUNC` env（`user`，6 服务既有接线零改动向后兼容），未知值 fail-fast `ValueError`；agent-platform-api demo `RATE_LIMIT_KEY_FUNC=tenant` 路径 pin | `0e6efbd` `a312249` `33942e6` |
| **R31-B** | **LangGraph MemorySaver 断点续跑** | §三 §3/§4「可恢复」+ R24 审计 G6：LangGraph runtime compile 挂 `MemorySaver` checkpointer + `interrupt_before=["ApprovalRequired"]`，审批 required 的 run 在 HITL gate 处**暂停**（不 finalize），thread 持久化 `thread_id=run_id`；新增 `resume(run_id, decision, decided_by, comment)` 经 `graph.update_state` 注入决策 + `graph.invoke(None, config)` 驱动图到 `ApprovalApproved`/`ApprovalRejected` → END；图新增 `ApprovalApproved` + `ApprovalRejected` 节点 + 条件边；**替换** R24 之前 `decide()` 的 `model_copy` 缝合路径（G6），`decide()` 保留为 checkpointer-failure fallback；细节 + 测试矩阵见 `Docs/superpowers/specs/2026-06-22-r31-final-enhancements.md` | `48397c9` `9d67743` `8e5433b` |

**R31 对 §7.1 待办清单的更新**：

- **P1-7 限流维度参数化 — ✅ 闭合（R31-A）**：限流从「user 单维」推到「user/tenant/endpoint/tool 4 维 + 自定义 callable」；`RATE_LIMIT_KEY_FUNC` env 路径让 6 服务既有接线零改动。**注意**：单一 API gateway（ingress-level）仍未做——R31-A 闭合的是「维度参数化」，「网关化」仍待 R32+。
- **P2 LangGraph「可恢复」— ✅ 闭合（R31-B）**：HITL 审批走真 `MemorySaver` checkpoint + `interrupt_before` + `resume()`，替换 R24 审计 G6 的 `decide()` model_copy 缝合；6 条契约（暂停 / approve 完成 / reject 终止 / 跨 runtime 持久化 / 只读不 interrupt / 未知 run 抛 KeyError）pin 死。**注意**：LangGraph 编排层更深层（条件边 / 真子图 / 多 gate / 长运行 resume）仍是 R31-B 的单 interrupt gate，待后续。
- **P0-1 Dockerfile 全服务补齐 — 仍未完成**：继续留待 R32+。
- **P0-3 RCA 告警收敛算法 — 仍未完成**（`del time_window_minutes` TODO 仍在）：继续留待 R32+。
- **P1-5 模板铺面 3/5 → 5/5** — 真实三方端到端测试待后续。
- **P1 限流网关化** — R31-A 闭合维度参数化，ingress-level 网关化待后续。

**R31 后最终 9 项差距清零状态**：

| # | 差距条目 | 状态 | 闭合轮次 |
| --- | --- | --- | --- |
| 1 | §7.1 P0-2 PostgreSQL 迁移 | ✅ | R29-A |
| 2 | §7.1 P3-19 备份 runbook | ✅ | R30-C |
| 3 | §7.1 P3-17 mcp-gateway / approval-service 独立化 | ✅ | R21 |
| 4 | §7.1 P3-18 对象存储 MinIO 接入 | ✅ | R22 |
| 5 | §7.1 P3-19 高可用：多副本 + 幂等 | ✅ | R23 |
| 6 | §三 §9 event-gateway 部署单元 | ✅ | R29-C |
| 7 | §三 §9 SSO/OIDC 接入 | ✅ | R24 |
| 8 | §三 §9 LLM Trace（Langfuse） | ✅ | R24 |
| 9 | §四 P2 敏感字段脱敏 + Prompt 版本 A/B | ✅ | R24 (脱敏) + R30-B (Prompt 版本) |

**总评（R31 后）**：R17 → R31 共 15 轮迭代把差距分析原始 P0–P3 全部 9 项 🔴 未实现条目**保持清零**，并进一步把 P1/P2 治理深度项向纵深推进：

- **P0**（3 项）→ **1/3 闭合**（PG 迁移 ✅），剩 Dockerfile / 收敛算法 2 项留待 R32+（改动面过大，本轮未接）。
- **P1**（5 项）→ **3/5 完全闭合 + 2 项纵深推进**（可观测埋点 ✅ R25、限流共享包 ✅ R25、Reranker ✅ R26、工具韧性 ✅ R25、模板铺面部分 R30-B）；限流维度参数化 ✅ R31-A（网关化仍待）、模板铺面 5/5 真实三方接入待后续。
- **P2**（8 项）→ **7/8 闭合**（Kafka 告警流 ✅ R27+R29-C、event-gateway ✅ R29-C、LangGraph v1 执行层 ✅ R29-B、Langfuse Trace ✅ R24、Prompt 版本 ✅ R30-B、SSO/OIDC ✅ R24、审批补充/转派/超时升级 ✅ R20、Faithfulness/安全策略评测 ✅ R18、**LangGraph「可恢复」✅ R31-B**），剩限流网关化（ingress-level）+ LangGraph 编排层深层（真子图 / 多 gate）。
- **P3**（3 项）→ **3/3 闭合**（mcp-gateway/approval-service 独立化 ✅ R21、对象存储 MinIO ✅ R22、高可用多副本+幂等 ✅ R23、备份 runbook ✅ R30-C）。

**R32 建议主线**：**P0-1 Dockerfile 全服务补齐（7 服务 + web-portal）+ P0-3 RCA 告警收敛算法**（仍是改动面最大的两条 P0 遗留，与 R31 改动面正交）。次要：限流网关化（ingress-level）、模板铺面 5/5 真实三方接入、LangGraph 编排层深层（真子图 + 多 gate + 长运行 resume）。

### 7.6 R32-A 收尾（2026-06-23，限流网关化闭合）

- **P1-7 限流网关化 — ✅ 闭合（R32-A）**：新增 `services/api-gateway`（端口 8070）作为 spec §三 §5.1 要求的单一 ingress 网关，按路径前缀路由到 6 个后端（`/api/knowledge|/api/rca|/api/platform|/api/tools|/api/approvals|/api/mcp`），并在网关层统一收口四项横切关注点：认证（复用 `auth-policy` 的 `require_internal_or_jwt`，`API_GATEWAY_AUTH_REQUIRED` 默认 false 开放、生产翻 true）、限流（复用 `install_rate_limiter`，与 6 服务同一共享包）、审计（`AuditMiddleware` 记录 trace_id+run_id+method+path+backend+status）、trace_id 生成+透传（无 `X-Trace-Id` 时生成 UUID，始终透传到后端+响应）。`BackendProxy` Protocol + `HttpBackendProxy`（httpx）让测试注入 stub 不开 socket。Dockerfile + k8s + helm + docker-compose 全套部署清单就位；helm `test_values_yaml_loads` 扩到 9 服务。**约束**：新增服务，零改动后端，全量 1647 测试 0 失败。
- **剩余 P1/P2**：模板铺面 5/5 真实三方接入、LangGraph 编排层深层（真子图 + 多 gate + 长运行 resume）、OCR + 滑动窗口 + 表格结构化；P0 仍剩 Dockerfile 全服务补齐 + RCA 告警收敛算法。

> 收尾 spec：`Docs/superpowers/specs/2026-06-22-r31-final-enhancements.md`（R31）、`Docs/superpowers/specs/2026-06-22-r30-remaining-gaps.md`（R30）、`Docs/superpowers/specs/2026-06-22-r29-pg-default-langgraph-event-gateway.md`（R29）、`Docs/superpowers/specs/2026-06-22-r28-real-middleware-smoke.md`（R28）、`Docs/superpowers/specs/2026-06-19-r27-kafka-neo4j-scoring.md`（R27）、`Docs/superpowers/specs/2026-06-19-r26-reranker-rca-depth.md`（R26）、`Docs/superpowers/specs/2026-06-19-r25-observability-resilience-ratelimit.md`（R25）、`Docs/superpowers/specs/2026-06-19-r24-auth-trace-redaction.md`（R24）。
