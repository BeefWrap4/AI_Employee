# agent-platform-api

Platform API for Agent runs, templates, approvals, audit records, and run trace queries.

## MVP Scope

This M5 slice provides an in-memory Agent Runtime API with three published templates:

- `knowledge_qa` - Knowledge QA Agent, read-only, no approval required.
- `rca` - RCA Agent, requires human approval before final write-back.
- `inspection` - Inspection Agent, read-only, no approval required.

## Endpoints

- `GET /health`
- `GET /api/v1/agent-templates`
- `POST /api/v1/agent-runs`
- `GET /api/v1/agent-runs?template_id=...&status=...&page=1&page_size=50`
- `GET /api/v1/agent-runs/{run_id}`
- `GET /api/v1/approval-tasks?status=...&page=1&page_size=50`
- `POST /api/v1/approval-tasks/{task_id}/decision`

RCA runs create a pending approval task. `approved` decisions complete the run; `rejected` decisions fail the run. Decided tasks cannot be changed.

## Local Run

```powershell
conda activate ai-employee
uvicorn ai_employee.agent_platform_api.app:app --port 8030 --app-dir services/agent-platform-api/src
```

This first platform slice does not introduce MCP/FastMCP yet. Tool calls are represented as structured run trace summaries so the portal and later tool-registry integration have a stable contract.
