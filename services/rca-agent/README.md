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

Set `RCA_SQLITE_PATH` to persist RCA runs, reports, and review decisions across app restarts:

```powershell
conda activate ai-employee
$env:RCA_SQLITE_PATH="./var/data/rca.sqlite3"
uvicorn ai_employee.rca_agent.app:app --port 8020 --app-dir services/rca-agent/src
```

## Replay Evaluation

Run the simulated RCA replay suite and emit a JSON report:

```powershell
conda activate ai-employee
python -m ai_employee.rca_agent.replay tests/rca-replay/sample_cases.jsonl --json
```

The report includes Top-1/Top-3 root-cause coverage, evidence coverage, average evidence count, and per-case predicted root-cause types.

Without `RCA_SQLITE_PATH`, the MVP uses in-memory state. Diagnostic tools are simulated and read-only; the service does not execute high-risk O&M actions.
