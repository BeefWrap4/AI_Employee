# AI Employee

本仓库用于存放与 AI Agent 工程师工作相关的项目材料与设计文档。

## 项目背景

围绕基站运维场景，从内部知识库智能问答，到告警根因分析 Agent，再到智能运维 Agent 平台，递进建设三个内部 AI 项目。

## 文档结构

`Docs/` 目录包含项目的设计与规划文档：

- **ai-agent-telecom-projects-development-timeline.md** — 三项目总体开发时间线与简历叙事
- **project-1-rag-knowledge-base-design-spec.md** — 项目一：基于 RAG 的内部知识库智能问答系统设计规格
- **project-2-base-station-alarm-rca-agent-design-spec.md** — 项目二：基站告警根因分析 Agent 设计规格
- **project-3-intelligent-ops-agent-platform-design-spec.md** — 项目三：智能运维 Agent 平台设计规格

## 目录约定

- `Docs/` — 项目设计文档与规划材料
- 后续业务代码、脚本、配置等将按需新增子目录

## 工具链

- 版本控制：Git
- 文档：Markdown

## 本地开发环境

推荐使用 Miniconda 创建独立 Python 环境：

```powershell
conda env create -f environment.yml
conda activate ai-employee
python -m pip install -e ".[dev]"
python -m pytest
```

启动知识库 M1 双服务（需两个进程）：

```powershell
conda activate ai-employee
# 终端 1：ingestion-worker
uvicorn ai_employee.ingestion_worker.app:app --port 8001 --app-dir services/ingestion-worker/src
# 终端 2：knowledge-api
uvicorn ai_employee.knowledge_api.app:app --port 8010 --app-dir services/knowledge-api/src
```

环境变量样例见 `.env.example`。M1 用 SQLite + Stub Embedding 零外部依赖即可运行；`EMBEDDING_PROVIDER=openai_compat` 可切换到真实 OpenAI-compatible 接口。

知识检索使用 `knowledge_scopes` 做 MVP 级权限过滤：`acl_tags` 为空的文档视为 public；`acl_tags` 非空的文档必须与请求中的 scope 或文档 metadata 值命中后才会进入召回和引用。

运行本地 M1 冒烟流程（上传、解析、发布、问答、反馈、审计查询）：

```powershell
conda activate ai-employee
python scripts/m1_smoke.py --json
```

启动 M3 RCA Agent 原型服务：

```powershell
conda activate ai-employee
uvicorn ai_employee.rca_agent.app:app --port 8020 --app-dir services/rca-agent/src
```

如需持久化 RCA run、报告和审核结果，可设置：

```powershell
$env:RCA_SQLITE_PATH="./var/data/rca.sqlite3"
```

运行 RCA 回放评测：

```powershell
conda activate ai-employee
python -m ai_employee.rca_agent.replay tests/rca-replay/sample_cases.jsonl --json
```

启动 M5 Agent Platform API 原型服务：

```powershell
conda activate ai-employee
uvicorn ai_employee.agent_platform_api.app:app --port 8030 --app-dir services/agent-platform-api/src
```
