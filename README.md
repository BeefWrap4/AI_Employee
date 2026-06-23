# AI Employee

面向电信运维场景的 AI Agent monorepo。项目按三阶段递进：RAG 知识库、基站告警 RCA Agent、智能运维 Agent 平台。当前目标是可运行 MVP，默认技术栈为 Python + FastAPI + React + Docker Compose。

## 项目结构

- `services/knowledge-api`：知识问答、文档索引与引用回答 API。
- `services/ingestion-worker`：文档解析、分段、向量化与索引构建。
- `services/rca-agent`：告警标准化、incident 聚合、根因候选与 RCA 报告。
- `services/agent-platform-api`：Agent 模板、运行记录、审批、评测与审计。
- `services/tool-registry`：工具注册、Schema、权限与健康检查。
- `apps/web-portal`：React + Ant Design 运维门户。
- `packages/`：共享 schema、鉴权、对象存储、网关等库。
- `tests/`：单元、集成、回放、评测和端到端测试。
- `Docs/`：设计规格、实施计划和验收文档。
- `infra/docker-compose`：本地 Docker 依赖与服务编排。

## Docker Compose 一键演示

启动并验证完整本地栈：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\docker-smoke.ps1 -Json
```

已有栈运行时只做验证：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\docker-smoke.ps1 -NoStart -Json
```

写入演示数据：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\seed-demo.ps1 -Json
```

打开前端：[http://127.0.0.1:5173](http://127.0.0.1:5173)

前端端到端验证：

```powershell
cd apps\web-portal
npm run e2e:docker
```

## 本地 Python 开发

推荐使用 Miniconda：

```powershell
conda env create -f environment.yml
conda activate ai-employee
python -m pip install -e ".[dev]"
pytest tests --ignore=tests/test_local_ci.py -q
```

常用服务启动：

```powershell
uvicorn ai_employee.knowledge_api.app:app --port 8010 --app-dir services/knowledge-api/src
uvicorn ai_employee.rca_agent.app:app --port 8020 --app-dir services/rca-agent/src
uvicorn ai_employee.agent_platform_api.app:app --port 8030 --app-dir services/agent-platform-api/src
```

## 前端开发

```powershell
cd apps\web-portal
npm install
npm run dev
npm run build
```

Vite 开发代理将 `/api/knowledge`、`/api/rca`、`/api/platform`、`/api/tools` 分别转发到本地后端服务。

## API 与验证入口

- Knowledge API：`/api/knowledge/api/v1/documents`、`/api/knowledge/api/v1/chat/query`
- RCA API：`/api/rca/api/v1/rca/runs`、`/api/rca/api/v1/rca/reports/{report_id}`
- Platform API：`/api/platform/api/v1/agent-runs`、`/api/platform/api/v1/agent-runs/{run_id}/trace`
- Tool Registry：`/api/tools/api/v1/tools`

更多验收步骤见 `Docs/mvp-acceptance-checklist.md`。
