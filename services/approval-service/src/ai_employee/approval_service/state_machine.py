"""Pure approval-task state machine (R20 governance flavour).

Mirrors the agent-platform ``runtime`` governance transitions but
operates **only** on a task — it never touches agent runs.  Run
side-effects remain the platform's responsibility, so this service can
be deployed and scaled independently (spec §9 ``approval-service``).

All functions take a task dict (the ``ApprovalTask`` payload shape) and
return the updated dict; the caller (``app.py``) persists it via the
store.  Exceptions encode the same error codes the platform surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class ApprovalTaskNotFound(LookupError):
    """Raised when a governance action targets an unknown task id."""


class ApprovalTaskNotSupplementable(ValueError):
    """Raised when a supplement is requested on a non-supplementable task."""


class ApprovalSupplementStateConflict(ValueError):
    """Raised when a supplement resolve is attempted in the wrong state."""


class ApprovalTransferForbidden(PermissionError):
    """Raised when a non-authorised user attempts to transfer a task."""


class ApprovalTaskNotModifiable(ValueError):
    """Raised when a decision targets a terminal / undecidable task."""


_TERMINAL_STATUSES = frozenset({"approved", "rejected", "expired"})
_DECIDABLE_STATUSES = {"pending", "transferred", "escalated"}


def is_decidable(task: dict[str, Any]) -> bool:
    """Return True when ``task`` may still receive a final decision."""
    return task["status"] in _DECIDABLE_STATUSES


def create_task(payload: dict[str, Any]) -> dict[str, Any]:
    now = payload.get("created_at") or datetime.now(timezone.utc).isoformat()
    approver = payload.get("current_approver") or payload["requested_by"]
    return {
        "task_id": payload["task_id"],
        "run_id": payload["run_id"],
        "template_id": payload["template_id"],
        "requested_by": payload["requested_by"],
        "status": "pending",
        "risk_level": payload.get("risk_level", "approval_required"),
        "reason": payload.get("reason", ""),
        "decided_by": None,
        "comment": None,
        "supplement_request": None,
        "supplement_response": None,
        "assignee": None,
        "routed_to": None,
        "deadline_at": None,
        "created_at": now,
        "updated_at": now,
        "delegates": [],
        "delegated_by": None,
        "supplement_note": None,
        "supplement_attachments": [],
        "supplement_requested_by": None,
        "supplement_resolved_by": None,
        "transfers": [],
        "current_approver": approver,
        "escalated_at": None,
        "escalated_to": None,
        "escalation_reason": None,
    }


def decide(
    task: dict[str, Any],
    *,
    decision: str,
    decided_by: str,
    comment: str | None,
) -> dict[str, Any]:
    if not is_decidable(task):
        raise ApprovalTaskNotModifiable(task["status"])
    now = datetime.now(timezone.utc).isoformat()
    return {
        **task,
        "status": decision,
        "decided_by": decided_by,
        "comment": comment,
        "updated_at": now,
    }


def request_supplement_governance(
    task: dict[str, Any],
    *,
    note: str,
    attachments: list[dict[str, Any]],
    requested_by: str,
) -> dict[str, Any]:
    if task["status"] not in ("pending",):
        raise ApprovalTaskNotSupplementable(task["status"])
    now = datetime.now(timezone.utc).isoformat()
    return {
        **task,
        "status": "supplement_pending",
        "supplement_note": note,
        "supplement_attachments": list(attachments),
        "supplement_requested_by": requested_by,
        "updated_at": now,
    }


def resolve_supplement_governance(
    task: dict[str, Any],
    *,
    attachments: list[dict[str, Any]],
    note: str | None,
    resolved_by: str,
) -> dict[str, Any]:
    if task["status"] != "supplement_pending":
        raise ApprovalSupplementStateConflict(task["status"])
    now = datetime.now(timezone.utc).isoformat()
    merged = [*task.get("supplement_attachments", []), *attachments]
    return {
        **task,
        "status": "pending",
        "supplement_attachments": merged,
        "supplement_response": note,
        "supplement_resolved_by": resolved_by,
        "updated_at": now,
    }


def transfer_approval(
    task: dict[str, Any],
    *,
    new_approver: str,
    reason: str,
    transferred_by: str,
    is_admin: bool = False,
) -> dict[str, Any]:
    authorised = (
        is_admin
        or transferred_by == task.get("current_approver")
        or transferred_by == task["requested_by"]
    )
    if not authorised:
        raise ApprovalTransferForbidden(transferred_by)
    if task["status"] in _TERMINAL_STATUSES:
        raise ApprovalTaskNotSupplementable(task["status"])
    now = datetime.now(timezone.utc).isoformat()
    history = list(task.get("transfers", []))
    history.append(
        {
            "from": task.get("current_approver") or task["requested_by"],
            "to": new_approver,
            "reason": reason,
            "transferred_by": transferred_by,
            "is_admin": is_admin,
            "ts": now,
        }
    )
    return {
        **task,
        "status": "transferred",
        "current_approver": new_approver,
        "transfers": history,
        "routed_to": new_approver,
        "updated_at": now,
    }


def escalate_approval(
    task: dict[str, Any],
    *,
    escalated_to: str | None,
    reason: str | None,
    escalated_by: str | None,
) -> dict[str, Any]:
    if task["status"] in _TERMINAL_STATUSES:
        raise ApprovalTaskNotSupplementable(task["status"])
    now = datetime.now(timezone.utc).isoformat()
    target = escalated_to or task.get("current_approver") or task["requested_by"]
    return {
        **task,
        "status": "escalated",
        "escalated_at": now,
        "escalated_to": target,
        "escalation_reason": reason,
        "current_approver": target,
        "routed_to": target,
        "updated_at": now,
    }


__all__ = [
    "ApprovalSupplementStateConflict",
    "ApprovalTaskNotFound",
    "ApprovalTaskNotModifiable",
    "ApprovalTaskNotSupplementable",
    "ApprovalTransferForbidden",
    "create_task",
    "decide",
    "escalate_approval",
    "is_decidable",
    "request_supplement_governance",
    "resolve_supplement_governance",
    "transfer_approval",
]
