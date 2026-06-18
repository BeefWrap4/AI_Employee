# R20 Approval Governance — State Machine

This document specifies the unified approval-task state machine
introduced by R20 (supplement, transfer, escalation) on top of the
M5 decision lifecycle.  It is the source of truth for
`ApprovalTask.status` transitions in `agent-platform-api`.

## States

The unified `ApprovalTaskStatus` enum:

| Status              | Meaning                                                        |
|---------------------|----------------------------------------------------------------|
| `pending`           | Open, waiting for a decision from the current approver.        |
| `supplement_pending`| Reviewer asked for more material; requester must respond. (R20-1) |
| `transferred`       | Reassigned to a new approver; still decidable. (R20-2)         |
| `escalated`         | SLA breached / stuck; routed to an escalation reviewer. (R20-3)|
| `approved`          | Final decision: approved. Terminal.                            |
| `rejected`          | Final decision: rejected. Terminal.                            |
| `expired`           | Legacy HITL timeout (`/approval-tasks/{id}/timeout`). Terminal.|
| `pending_supplement`| Legacy HITL supplement (`/approval-tasks/{id}/supplement-request`). |

The first five are the R20 governance track; the last three preserve
backward compatibility with the M5 / HITL extension flows.

## Transition Diagram

```
                      ┌─────────────────────────────────────────────┐
                      │                                             │
                      ▼                                             │
create_run ──► pending ──► approved   (decision: approved, terminal)
                │  │  │
                │  │  └──► rejected   (decision: rejected, terminal)
                │  │
                │  ├──► supplement_pending ──► pending   (R20-1 resolve)
                │  │
                │  ├──► transferred ──► pending            (R20-2 pickup)
                │  │        │
                │  │        └──► approved / rejected       (new approver decides)
                │  │
                │  └──► escalated ──► pending              (R20-3, escalation reviewer acts)
                │           │
                │           └──► approved / rejected       (escalation reviewer decides)
                │
                └──► expired  (legacy /approval-tasks/{id}/timeout, terminal)
```

Legacy HITL supplement (`/approval-tasks/{id}/supplement-request`) uses
the parallel `pending_supplement` status and is unaffected by R20.

## R20-1 Supplement

- Endpoint: `POST /api/v1/approvals/{task_id}/supplement`
  Body: `{note, attachments[], requested_by}` → `pending` → `supplement_pending`.
- Endpoint: `POST /api/v1/approvals/{task_id}/supplement/resolve`
  Body: `{attachments[], note?, resolved_by}` → `supplement_pending` → `pending`.
- Errors: 404 `approval_task_not_found`, 409
  `approval_task_not_supplementable` (already decided) /
  `not_supplement_pending` (resolve on wrong state), 422 (missing `note`).

## R20-2 Transfer (reassign)

- Endpoint: `POST /api/v1/approvals/{task_id}/transfer`
  Body: `{new_approver, reason, transferred_by, is_admin?}` →
  `pending|transferred|escalated` → `transferred`.
- Permission: `transferred_by` must equal `current_approver` /
  `requested_by`, or `is_admin=true`. Otherwise 403
  `approval_transfer_forbidden`.
- History: every transfer appends `{from, to, reason, transferred_by,
  is_admin, ts}` to `transfers` (chronological). `current_approver` and
  `routed_to` update to `new_approver`.
- A `transferred` task is still decidable by the new approver (returns
  to `pending` semantics for the decision guard).
- Errors: 404, 403, 409 `approval_task_not_transferable` (terminal
  state), 422 (missing `reason`).

## R20-3 Escalation

- Manual: `POST /api/v1/approvals/{task_id}/escalate`
  Body: `{escalated_to?, reason?, escalated_by?}` → `pending|transferred`
  → `escalated`. `escalated_to` defaults to `current_approver`.
- Background sweep: `escalate_overdue_approvals(store,
  timeout_seconds?, escalate_to?, notifier?)` escalates every `pending`
  task older than `APPROVAL_TIMEOUT_SECONDS` (default 3600s), then
  notifies the escalation reviewer via `notify_escalation_reviewer`.
- An `escalated` task is still decidable by the escalation reviewer.
- Errors: 404, 409 `approval_task_not_escalatable` (terminal state).

## Decision Guard (unified)

`runtime.is_decidable(task)` returns `True` for `pending`,
`transferred`, `escalated`.  The `/approval-tasks/{id}/decision`
endpoint uses this guard so governance sub-states remain decidable.
`supplement_pending` / `pending_supplement` must be resolved first;
`approved` / `rejected` / `expired` return 409
`approval_task_already_decided`.

## Run-level approval_status

`AgentRunResponse.approval_status` (`ApprovalStatus`) mirrors the task
status minus the run-level `not_required` sentinel.  The decision
endpoint still sets the run to `completed` (approved) or `failed`
(rejected); governance sub-states do not mutate the run — only a final
decision does.
