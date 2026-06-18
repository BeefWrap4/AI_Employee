"""approval_tasks table for the approval-service (R21)

Revision ID: 0002_approval_tasks
Revises: 0001_baseline
Create Date: 2026-06-19

Adds the ``approval_tasks`` table owned by the standalone
``approval-service`` (spec §9).  The service's in-process
``ApprovalTaskStore.init_schema`` creates the same table with
``CREATE TABLE IF NOT EXISTS`` for dev/test bootstrap; this migration
is the authoritative, version-controlled path.

Dialect-aware (R16-2 pattern): runs against both SQLite (dev/test) and
PostgreSQL (prod).  JSON-valued fields (``delegates``,
``supplement_attachments``, ``transfers``) are stored as ``TEXT`` /
``JSONB``:

* SQLite → ``TEXT`` (the store (de)serialises with ``json``).
* Postgres → ``JSONB`` so the columns are queryable natively.

The ``TEXT`` primary key (``task_id``) and ``TEXT`` timestamps are
portable across both dialects, so no autoincrement branching is
needed.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_approval_tasks"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """True when the active connection is PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def _json_type(postgres: bool) -> str:
    return "JSONB" if postgres else "TEXT"


def upgrade() -> None:
    postgres = _is_postgres()
    json_t = _json_type(postgres)
    # NOTE: f-string interpolation is safe here — the only interpolated
    # value is a fixed dialect discriminator ("JSONB" / "TEXT"), never
    # user input.
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS approval_tasks (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            template_id TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            status TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            reason TEXT NOT NULL,
            decided_by TEXT,
            comment TEXT,
            supplement_request TEXT,
            supplement_response TEXT,
            assignee TEXT,
            routed_to TEXT,
            deadline_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            delegates_json {json_t} NOT NULL DEFAULT '[]',
            delegated_by TEXT,
            supplement_note TEXT,
            supplement_attachments_json {json_t} NOT NULL DEFAULT '[]',
            supplement_requested_by TEXT,
            supplement_resolved_by TEXT,
            transfers_json {json_t} NOT NULL DEFAULT '[]',
            current_approver TEXT,
            escalated_at TEXT,
            escalated_to TEXT,
            escalation_reason TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_tasks_status "
        "ON approval_tasks(status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_tasks_run "
        "ON approval_tasks(run_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_approval_tasks_run")
    op.execute("DROP INDEX IF EXISTS idx_approval_tasks_status")
    op.execute("DROP TABLE IF EXISTS approval_tasks")
