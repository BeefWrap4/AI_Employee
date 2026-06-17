# rca-agent

FastAPI service for the M3 RCA Agent MVP. It normalizes alarm events, builds incidents, executes a controlled in-memory RCA DAG, collects simulated evidence, ranks root-cause hypotheses, generates Markdown reports, and records human review decisions.

## Endpoints

- `GET /health`
- `POST /api/v1/alarms/events` - normalize one raw alarm payload.
- `POST /api/v1/incidents/build` - group replayed alarms into one incident.
- `POST /api/v1/rca/runs` - create a synchronous RCA run from `incident_id` or replayed `alarms`.
- `GET /api/v1/rca/runs/{run_id}` - fetch run status, state history, evidence, and hypotheses.
- `GET /api/v1/rca/reports/{report_id}` - fetch Markdown report, evidence, hypotheses, and review status.
- `POST /api/v1/rca/reports/{report_id}/review` - record an expert review decision.

## Local Run

```powershell
conda activate ai-employee
uvicorn ai_employee.rca_agent.app:app --port 8020 --app-dir services/rca-agent/src
```

The MVP uses in-memory state and simulated read-only diagnostic tools. It does not execute high-risk O&M actions.
