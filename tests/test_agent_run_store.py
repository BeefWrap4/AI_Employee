"""AgentRunStore persistence tests."""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.run_store import AgentRunStore


def _sample_run(run_id: str = "agent_run_001") -> dict:
    return {
        "run_id": run_id,
        "template_id": "knowledge_qa",
        "agent_name": "Knowledge QA Agent",
        "status": "waiting_approval",
        "trace_id": "trace_agent_run_001",
        "requested_by": "alice",
        "input": {"question": "What is RRC?"},
        "output": {"summary": "draft", "citations": []},
        "node_trace": [
            {"node_name": "TemplateLoaded", "status": "completed", "detail": "ok"}
        ],
        "tool_calls": [
            {"tool_name": "knowledge-api.chat.query", "risk_level": "read_only",
             "status": "completed"}
        ],
        "approval_status": "pending",
    }


def test_upsert_and_get_roundtrip(tmp_path) -> None:
    store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    store.upsert_run(_sample_run())
    record = store.get_run("agent_run_001")
    assert record is not None
    assert record["template_id"] == "knowledge_qa"
    assert record["input"]["question"] == "What is RRC?"
    assert record["node_trace"][0]["node_name"] == "TemplateLoaded"


def test_upsert_updates_existing_run(tmp_path) -> None:
    store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    store.upsert_run(_sample_run())
    updated = _sample_run()
    updated["status"] = "completed"
    updated["approval_status"] = "approved"
    store.upsert_run(updated)
    record = store.get_run("agent_run_001")
    assert record["status"] == "completed"
    assert record["approval_status"] == "approved"


def test_upsert_appends_events(tmp_path) -> None:
    store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    payload = _sample_run()
    payload["new_events"] = [
        {"node_name": "RunStarted", "status": "completed", "detail": "started"},
        {"node_name": "ToolPlan", "status": "completed", "detail": "planned"},
    ]
    store.upsert_run(payload)
    record = store.get_run("agent_run_001")
    assert [e["node_name"] for e in record["events"]] == ["RunStarted", "ToolPlan"]


def test_mark_resumed_clears_resume_token(tmp_path) -> None:
    store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    store.upsert_run(_sample_run())
    store.mark_resumed("agent_run_001", resume_from_node="ApprovalRequired")
    record = store.get_run("agent_run_001")
    assert record["status"] == "running"
    assert record["resume_from_node"] == "ApprovalRequired"


def test_list_runs_paginates(tmp_path) -> None:
    store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    for i in range(1, 4):
        store.upsert_run(_sample_run(run_id=f"agent_run_{i:03d}"))
    rows, total = store.list_runs(page=1, page_size=2)
    assert total == 3
    assert len(rows) == 2
    rows, total = store.list_runs(page=2, page_size=2)
    assert len(rows) == 1


def test_get_run_returns_none_when_missing(tmp_path) -> None:
    store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    assert store.get_run("does_not_exist") is None
