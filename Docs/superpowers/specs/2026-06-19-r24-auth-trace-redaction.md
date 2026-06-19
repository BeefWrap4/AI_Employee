# Spec — R24 收尾：Auth/OIDC + LLM Trace + Audit/Redaction

日期：2026-06-19
主题：R24 阶段交付总览——**Auth/OIDC（a 域）**、**LLM Trace/Langfuse（b 域）**、**Audit/Redaction（c 域）** 三域合并 + 后置 ruff 清理，共 17 个提交
适用范围：`packages/auth-policy`、`packages/common-schemas`、`packages/llm-gateway`、`packages/observability`、`services/agent-platform-api`、`services/knowledge-api`、`services/rca-agent`、`infra/helm`、`infra/k8s`、`.env.example`、`tests/`

## 1. 目标 (Goal)

`Docs/gap-analysis-2026-06-18.md` §三 §9 标 🔴 的「LLM Trace（Langfuse/LangSmith）+ SSO/OIDC」与 §四 P2 治理深度的「脱敏 + Prompt 版本」在 R19–R23 中未触及，R22 收尾已明确为 R24+ 候选。R24 一次性把三条治理主线补齐：

1. **Auth/OIDC（a 域）**：把 mock 的 HS256 token 验证换成真 RS256 + JWKS，支持 OIDC IdP 接入；同时把 `require_jwt` 扩成 `require_oidc_or_internal`，让 OIDC 走线、HS256 JWT、内部 token 三条路径在同一权限框架下生效。
2. **LLM Trace/Langfuse（b 域）**：把 `LlmClient` 默认接入 Langfuse emitter；knowledge-api 的查询改写、RCA 推理、agent-platform 的 create_run 全部走 trace；token usage 真实上报（不再用 latency 顶替）；`parent_trace_id` 跨服务传递。
3. **Audit/Redaction（c 域）**：在 LLM 提示/响应、tool_call_log、RCA ticket writeback、agent platform audit 四个出口统一挂上 `redact_dict`，把 IMSI、密码等敏感字段擦除后再落库/外发。

> R23 收尾建议的 R24 主线是「PostgreSQL 默认化 + 真实中间件集成测试」（架构补全）。本轮 R24 实际选了**治理深度**主线（差距分析 §三 🔴 两项 + §四 P2），因为 (a) 治理深度的代码侵入面集中在 R23 已交付的 fastapi 依赖、llm-gateway client、rca 写回、agent platform audit 等少量模块，与 R23 HA 改动正交；(b) PG 迁移是单独的数据库工程，需独立 staging 验证，本轮先做纯代码闭环；(c) 三域互不阻塞、可并行（3 个 merge commit 分别合并），先把治理框架立起来，R25+ 再接 PG / 真实中间件 / SSO 端到端演练。

## 2. 三域交付总览

| 域 | 分支 | 主题 | 关键交付 | 合并提交 |
| --- | --- | --- | --- | --- |
| **a — Auth/OIDC** | r24-a.1+2 → a.6 | 真 RS256 验签 + JWKS 刷新 + OIDC 依赖 + 端点接线 + Helm/k8s/.env 模板 | `auth_policy/oidc.py` 重写、`fastapi_dep.require_oidc_or_internal`、3 个端点（agent-platform 写端点 + knowledge-api 文档写入 + rca-agent 写端点）注入权限 | `adb6310` |
| **b — LLM Trace** | r24-b.1 → b.5 | LlmClient 默认 Langfuse + knowledge-api trace + parent_trace_id + 真 token usage + LangGraph runtime 接线 | `llm_gateway/client.py` 默认 emitter + 父 trace + 真 token；`langfuse_emitter.py` 真实 usage；`agent_platform_api/runtime.py` 接入 LangGraph | `b5f19c9` |
| **c — Audit/Redaction** | r24-c-1 → c.5 | 递归 redact_dict + 4 个出口接线（LLM / tool_call / RCA ticket / audit） | `common_schemas.redaction` 递归规则；`llm_gateway` / `rca_agent.ticket_writeback` / `agent_platform_api.audit` / 两处 `tool_call_log` 全部走 `redact_dict` | `3fccd28` |
| 收尾 | 57568a7 | ruff format + auth headers for e2e | 14 个测试文件加 `Authorization: Bearer <internal>` 头，ruff 格式化 | `57568a7` |

合计 +3244 行 / -214 行（37 个文件改动），其中 14 个 R24 测试文件 + 1 个生产 redaction 拓展，共 +1634 行测试代码。

## 3. 实现清单 (Deliverables)

### 3.1 a 域：Auth/OIDC（r24-a.1+2 → a.6）

| 提交 | 文件 | 变更要点 |
| --- | --- | --- |
| `04d8dbb` a.1+2 | `packages/auth-policy/.../oidc.py` + `tests/test_oidc_signature.py` | `verify_jwt` 走真 RS256（`jwt.decode(..., algorithms=["RS256"], options={"verify_signature": True, "verify_aud": True, ...})`）——篡改 payload、错签名 key、未知 kid 都在验签阶段被拒，根本走不到 claim 校验。`RemoteJwksClient` 加 TTL 缓存 + **refresh-on-kid-miss**（缓存里没找到 kid 时主动失效重拉一次），key 轮换不再需要等 TTL 过期或重启 pod。新增 317 行真 RS256 测试：`cryptography` 生成真 RSA keypair，导出 JWKS，私钥签 token，断言「合法签验通过 / 篡改拒 / 错 key 拒 / 未知 kid 拒」。原有 20 个 `test_oidc_sso.py` + 54 个 auth 测试无修改全绿。 |
| `53dbade` a.3 | `packages/auth-policy/.../fastapi_dep.py` + `tests/test_oidc_internal_dep.py` | 新增 `require_oidc_or_internal(permissions=[...])` FastAPI 依赖。解析顺序：OIDC RS256 → HS256 JWT → 内部 token（与 `require_jwt` 共用 `can_any` 权限判断，endpoint 级 `permissions=[...]` 用法不变）。10 个新测试覆盖完整解析顺序、权限 enforcement、strict 模式、端到端 OIDC happy path（真 RS256 keypair）。 |
| `839ad8f` a.4 | `services/agent-platform-api/app.py` + `services/knowledge-api/app.py` + `services/rca-agent/app.py` + `tests/conftest.py` + `tests/test_knowledge_api_auth_wiring.py` | 三个服务的关键生产写端点接入 `require_oidc_or_internal`：agent-platform（agent-runs / evaluations / approvals / upload / documents 写）、knowledge-api（`POST /api/v1/documents`）、rca-agent（tickets / RCA report 写）。`scripts/m1_smoke.py` 走内部 token 注入。`tests/conftest.py` + `test_knowledge_api_auth_wiring.py`（318 行）验证每个端点的鉴权路径。提交信息记录全量 **1419 passed / 12 skipped / 0 failed**。 |
| `f3ec492` a.5 | `.env.example` | 新增 `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` 四个变量（注释中标注：OIDC 默认关闭，集群半配时降级 HS256 JWT + 内部 token 而非锁死）。 |
| `d8cd3f1` a.6 | `infra/helm/templates/secret.yaml` + `infra/helm/values.yaml` + `infra/k8s/secret.yaml` | Helm 模板读 `.Values.global.secrets.oidcIssuer / oidcClientId / oidcAudience / oidcJwksUrl`（默认空），values 注释解释用 values overlay 接线 IdP。`infra/k8s/secret.yaml` 同步。Helm 模板测试 15 个全绿。 |

### 3.2 b 域：LLM Trace/Langfuse（r24-b.1 → b.5）

| 提交 | 文件 | 变更要点 |
| --- | --- | --- |
| `72bc665` b.1 | `packages/llm-gateway/.../client.py` + `tests/test_r24_b_llm_client_default_emitter.py` | `LlmClient.__init__` 默认 emitter = `build_langfuse_emitter()`（None 时也建空 emitter 占位），不再「外部不传就裸发」。98 行测试覆盖默认 emitter 路径。 |
| `24b186b` b.2 | `services/knowledge-api/app.py` + `services/knowledge-api/query_rewriter.py` + `tests/test_r24_b_knowledge_trace.py` | knowledge-api 的查询改写链路接入 trace；`query_rewriter` 显式 emit parent trace / span；240 行测试覆盖整条 trace 链路。 |
| `76684d4` b.3 | `packages/llm-gateway/.../client.py` + `tests/test_r24_b_parent_trace_id.py` | `LlmClient.chat()` 接受 `parent_trace_id` 参数，跨服务把上游 trace id 透传下去；125 行测试。 |
| `7f29caa` b.4 | `packages/llm-gateway/.../client.py` + `packages/observability/.../langfuse_emitter.py` + `tests/test_r24_b_emitter_usage.py` | token usage 真实上报（不再用 `latency_ms` 顶替 input/output tokens），`langfuse_emitter` 字段对齐 OpenAI usage 块（`prompt_tokens` / `completion_tokens` / `total_tokens`）。143 行测试。 |
| `d18e552` b.5 | `services/agent-platform-api/app.py` + `services/agent-platform-api/runtime.py` + `tests/test_r24_b_runtime_wiring.py` | `runtime.create_run` 路径接入 LangGraph runtime，trace 与 LangGraph execution 同一上下文；`create_app` 默认从 runtime 工厂拉 LangGraph 实例。181 行测试。 |

### 3.3 c 域：Audit/Redaction（r24-c-1 → c.5）

| 提交 | 文件 | 变更要点 |
| --- | --- | --- |
| `0d08260` c-1 | `packages/common-schemas/.../redaction.py` + `tests/test_redaction.py` | `redact_dict(payload, rules)`：递归遍历 dict/list，命中 `rules`（IMSI 字段名、password 字段名、secret / token / api_key 子串）→ 替换为 `"***REDACTED***"`，递归保留结构（不破坏 list 顺序、不丢非敏感字段）。91 行测试覆盖嵌套 / 列表 / 命中规则 / 不命中规则 / 未知字段。 |
| `41d625c` c-2 | `packages/llm-gateway/.../client.py` + `tests/test_llm_gateway.py` | LLM gateway 的 trace emit 之前对 `prompt` / `response` 走 `redact_dict`（rules = `["imsi", "password", "secret", "token", "api_key"]`），外发到 Langfuse 之前擦除。84 行测试。 |
| `c484f02` c-3 | `services/rca-agent/.../ticket_writeback.py` + `tests/test_ticket_writeback.py` | RCA ticket 写回（summary / root_cause）落库前走 `redact_dict`；65 行测试。 |
| `a524736` c-4 | `services/agent-platform-api/.../audit.py` + `tests/test_audit_log.py` | agent platform 的 `audit.record()` 在 payload 落库前递归 `redact_dict`（保留外层 envelope：actor / action / target 不脱敏，仅 payload 内部擦除）。108 行测试。 |
| `34de564` c-5 | `services/agent-platform-api/.../tool_call_log.py` + `services/rca-agent/.../tool_call_log.py` + 2 个测试 | `tool_call_log.record()` 入库前对 `args` / `result` 递归 `redact_dict`；两个服务的 `tool_call_log` 走同一规则。29 + 29 = 58 行测试。 |

### 3.4 收尾 (post-merge cleanup)

| 提交 | 文件 | 变更要点 |
| --- | --- | --- |
| `57568a7` style | 14 个测试文件 + ruff format | (1) 14 个 R24 测试文件加 `Authorization: Bearer <internal>` 头（e2e 走 `require_oidc_or_internal` 后必须带 token）；(2) ruff 全量格式化 R24 改动文件，消除风格告警。 |

### 3.5 测试矩阵

| 测试文件 | 行数 | 覆盖点 |
| --- | --- | --- |
| `tests/test_redaction.py` (+91) | 99 | `redact_dict` 递归 / 嵌套 / 列表 / 命中 / 不命中 / 未知字段 |
| `tests/test_oidc_signature.py` | 317 | 真 RS256 keypair、合法 / 篡改 / 错 key / 未知 kid 拒签、JWKS refresh-on-kid-miss |
| `tests/test_oidc_internal_dep.py` | 302 | `require_oidc_or_internal` 解析顺序、权限 enforcement、strict、e2e OIDC happy path |
| `tests/test_oidc_sso.py` (更新) | 64++ | HS256 路径回归无破坏 |
| `tests/test_knowledge_api_auth_wiring.py` (新) | 318 | knowledge-api 端点鉴权 |
| `tests/test_r24_b_llm_client_default_emitter.py` (新) | 99 | LlmClient 默认 emitter |
| `tests/test_r24_b_parent_trace_id.py` (新) | 125 | parent_trace_id 透传 |
| `tests/test_r24_b_emitter_usage.py` (新) | 144 | token usage 真实上报 |
| `tests/test_r24_b_runtime_wiring.py` (新) | 181 | LangGraph runtime create_run 接线 |
| `tests/test_r24_b_knowledge_trace.py` (新) | 252 | knowledge-api trace 端到端 |
| `tests/test_audit_log.py` (+108) | 108 | agent platform audit 递归脱敏 |
| `tests/test_rca_tool_call_log.py` (+29) | 56 | rca tool_call_log 脱敏 |
| `tests/test_tool_call_log.py` (+29) | 106 | agent platform tool_call_log 脱敏 |
| `tests/test_ticket_writeback.py` (+65) | 65 | RCA ticket 写回脱敏 |
| `tests/test_llm_gateway.py` (+84) | +84 | LLM trace emit 脱敏 |

合计 +1634 行测试代码（14 个 R24 测试文件 / 新增 + 更新）。所有新增路径走真 RS256 + 真 Langfuse 桩（in-process emitter），CI 无需起 IdP / Langfuse 服务。

## 4. 测试结果 (Test Results)

- a 域提交 `839ad8f` 提交信息记录：**1419 passed, 12 skipped, 0 failed**（在 a 域 + b 域 + c 域 + 收尾合并前，R23 基础上 +3 个新测试文件全绿）。
- 收尾合并后 (`57568a7`) 提交信息未单独记录全量数；按 R24 改动面推算应维持 / 略超 1419 passed。
- 静态检查：`ruff check` / `ruff format` 在 R24 改动文件上无新增告警（`57568a7` 已处理）。
- Helm 模板测试 (`test_helm_templates.py`) 15 个全绿（`d8cd3f1` 后未变更）。
- 端到端手测：R24 各域 commit message 描述「e2e OK」，但未在真 IdP / 真 Langfuse 集群下跑过（CI 用 `cryptography` 真 RS256 keypair + in-process Langfuse emitter）。
- **未运行**：本工作树无 Python 环境，未本地复跑 `pytest`。CI 验证依赖 PR 后 GitHub Actions 跑通。

## 5. 已知遗留 (Known Gaps)

- **OIDC 端到端未演练**：真 IdP（Keycloak / Auth0 / Okta）端到端登录 → token 签发 → 服务验证的全链路未跑过；CI 用 `cryptography` 模拟 RS256，prod 上线前需在 staging 接真 IdP 验一遍。Helm 模板只暴露配置占位，values overlay 接线文档尚未补。
- **Langfuse 后端未生产化**：`build_langfuse_emitter()` 默认走空 emitter，Langfuse 服务地址 / API key 走 env 但未在 `.env.example` 暴露；CI / dev 环境 Langfuse 不可达时不会失败（emitter 设计上降级为 no-op），生产未接 Langfuse 时等于「默默无 trace」。
- **脱敏规则集保守**：`redact_dict` 仅按字段名子串匹配（`imsi` / `password` / `secret` / `token` / `api_key`），未做内容正则（不匹配「15 位纯数字」是 IMSI 之类）。用户输入中嵌入的明文密码（如对话里贴出密码字符串）不会被擦除，依赖上游对话层做内容脱敏。
- **tool_call_log 脱敏仅入参/出参**：`tool_call_log.record()` 只对 `args` / `result` 走 redact；`tool_name` / `target` 不脱敏（设计选择：工具名 + 目标对象保留可观测性，敏感载荷只擦「内容」）。
- **a 域 + b 域 + c 域未做集成冒烟**：三域独立通过单测，但「OIDC 鉴权通过的请求 → LLM trace → audit + tool_call_log 全部走 redact」的端到端没有专项测试。下一个 R25 写一个跨域 e2e 能把这事锁住。
- **`agent-platform-api` `create_run` 路径接 LangGraph runtime，但 LangGraph 状态机本身的回放 / 中断 / 重试语义未在本轮深入**（R24 b.5 只做 wiring，不做 LangGraph 深度集成）。LangGraph 仍处于「能跑通」状态，复杂 multi-agent 编排待 R25+。
- **未触及 P0-2 PostgreSQL 迁移**：R24 选了治理深度主线，PG 迁移未做；R23 HA 文档已明确 `pg_store` 默认化是抬 `knowledge-api` 副本的前置条件，本轮仍未解决。

## 6. 下一步建议 (R25+ Candidates)

R24 闭合了差距分析 §三 🔴 中「SSO/OIDC + LLM Trace」两项 + §四 P2 中「脱敏 + Prompt 版本」的脱敏子项。剩余候选按价值/工作量：

1. **PostgreSQL 成为默认元数据后端**（P0，高价值，中工作量）—— 让 `knowledge-api` / `approval-service` / `mcp-gateway` 真正 HA-safe 抬副本，是 R23 / R24 收尾反复标为「下一轮最高优先级」的悬空项。
2. **真实中间件集成测试**（中价值，中工作量）—— 在 CI 起 Postgres + Redis + MinIO + 真 Langfuse + 假 IdP 容器，跑一次真多副本 leader failover + 真 OIDC 鉴权 + 真 LLM trace 端到端。
3. **OIDC 真 IdP 演练 + SSO 端到端**（高价值，中工作量）—— staging 接 Keycloak / Auth0，从浏览器登录 → 拿 token → 调服务 → 落 trace → 脱敏入库走一遍；补 Helm values overlay 文档。
4. **LangGraph 深度集成**（中价值，中工作量）—— 状态机回放 / 中断 / 重试 / multi-agent 编排；与 R24 b.5 的 wiring 衔接。
5. **Reranker 二阶段重排**（P1-4，渐进）—— cross-encoder / bge-reranker，叠在 RAG 召回后重排。
6. **限流网关化**（P1-7）—— `SlidingWindowLimiter` Redis 后端就位，补到 `agent-platform-api` 网关层。
7. **工具健康检查 / 超时 / 熔断**（P1-6 剩余）—— `ToolSpec.timeout_ms` / `retry_policy` / `health_status` 主动探活。
8. **备份与恢复 runbook**（P2 / P3 备份子项）—— Postgres `pg_dump` / Redis RDB / MinIO bucket 复制，配套 restore 演练脚本。
9. **RCA 告警收敛算法**（P0-3，未完成）—— `del time_window_minutes` TODO 仍在。
10. **Dockerfile**（P0-1）—— k8s manifest 引用镜像仍无构建文件，部署到真集群的最后一公里。

建议 R25 主线接 **1 + 3**（PG 默认化 + OIDC 真 IdP 演练）：PG 解决 R23 / R24 反复标记的 HA 悬空，OIDC 真演练把 R24 a 域从「代码闭环」推到「staging 闭环」，互相不阻塞（PG 走数据库 + Alembic 迁移脚本，OIDC 走 Helm values + staging 演练脚本）。R25+ 再接 2 / 4 / 5 / 6。
