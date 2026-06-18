# approval-service

Standalone approval-task persistence + state machine (spec §9
deployable unit `approval-service`).

Owns:

- approval task persistence (`ApprovalTaskStore` → SQLite; the
  `0002_approval_tasks` Alembic migration adds the same table for
  Postgres with dialect-aware DDL)
- the unified approval state machine (R20 governance flavour):
  `pending → approved | rejected`, plus `supplement_pending`,
  `transferred`, `escalated` sub-states
- governance endpoints: supplement / transfer / escalation

It deliberately does **not** own agent run state — run side-effects
(complete/fail the run, append node trace) remain the
`agent-platform-api`'s responsibility. The platform delegates approval
calls here over HTTP via `ApprovalServiceClient` (see
`agent_platform_api/clients.py`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/approval-tasks` | create a task |
| GET  | `/api/v1/approval-tasks` | list (status filter + paging) |
| GET  | `/api/v1/approval-tasks/{id}` | fetch one |
| POST | `/api/v1/approval-tasks/{id}/decision` | approve / reject |
| POST | `/api/v1/approvals/{id}/supplement` | request supplementary material |
| POST | `/api/v1/approvals/{id}/supplement/resolve` | supply the material |
| POST | `/api/v1/approvals/{id}/transfer` | reassign to a new approver |
| POST | `/api/v1/approvals/{id}/escalate` | escalate an overdue task |

Contracts mirror the agent-platform R20 governance endpoints so
consumers can switch to the service with no contract drift.

## Run

```bash
docker build -f services/approval-service/Dockerfile -t ai-employee/approval-service .
docker run -p 8040:8040 -e PLATFORM_DATA_DIR=/var/data ai-employee/approval-service
```
