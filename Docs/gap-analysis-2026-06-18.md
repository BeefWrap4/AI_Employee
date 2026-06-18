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
