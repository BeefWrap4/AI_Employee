"""approval-service: standalone approval task persistence + state machine.

Spec §9 lists ``approval-service`` as an independent deployable unit.
It owns:

* approval task persistence (``ApprovalTaskStore`` → SQLite/Postgres)
* the unified approval state machine (R20 governance flavour)
* governance endpoints: supplement / transfer / escalation

It deliberately does NOT own agent run state — run side-effects remain
the agent-platform's responsibility.  The platform delegates approval
calls to this service over HTTP (see ``agent_platform_api.clients``).
"""

from __future__ import annotations

from ai_employee.approval_service.app import create_app, app
from ai_employee.approval_service.store import ApprovalTaskStore

__all__ = ["ApprovalTaskStore", "app", "create_app"]
