"""Platform tool_call_log tests (spec §5.3 + §6.4).

Persists every tool invocation (run_id, tool_name, input, output,
status, latency_ms, error_code) — drives tool_call_success_rate and
underpins audit query.  RCA already has its own store; this one is
platform-scoped.
"""
from __future__ import annotations

import os

import pytest
from ai_employee.agent_platform_api.tool_call_log import (
    PlatformToolCallLogStore,
    ToolCallRecord,
)


@pytest.fixture
def store(tmp_path: pytest.TempPathFactory) -> PlatformToolCallLogStore:
    db = os.path.join(str(tmp_path), "tclog.sqlite3")
    return PlatformToolCallLogStore(db_path=db)


def test_record_and_list(store: PlatformToolCallLogStore) -> None:
    store.record(
        run_id="run-1", tool_name="cmdb.lookup",
        input_summary='{"id":"BJ-001"}', output_summary='{"name":"BJ-001"}',
        status="success", latency_ms=42, error_code=None,
    )
    rows = store.list_for_run("run-1")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "cmdb.lookup"
    assert rows[0]["status"] == "success"
    assert rows[0]["latency_ms"] == 42


def test_success_rate_per_tool(store: PlatformToolCallLogStore) -> None:
    for i, status in enumerate(["success", "success", "failure"]):
        store.record(
            run_id=f"r{i}", tool_name="x",
            input_summary="", output_summary="",
            status=status, latency_ms=10,
            error_code=None if status == "success" else "boom",
        )
    rate = store.success_rate(tool_name="x")
    assert abs(rate - (2 / 3)) < 1e-6


def test_success_rate_unknown_tool_returns_default(store: PlatformToolCallLogStore) -> None:
    # No records → returns 1.0 (no signal).
    assert store.success_rate(tool_name="never-called") == 1.0


def test_latency_p95_per_tool(store: PlatformToolCallLogStore) -> None:
    for i in range(20):
        store.record(
            run_id=f"r{i}", tool_name="x",
            input_summary="", output_summary="",
            status="success", latency_ms=i * 10,
            error_code=None,
        )
    p95 = store.latency_p95(tool_name="x")
    # Near the top of the range.
    assert p95 >= 180


def test_failure_breakdown_per_tool(store: PlatformToolCallLogStore) -> None:
    for code in ["timeout", "timeout", "boom"]:
        store.record(
            run_id="r", tool_name="x",
            input_summary="", output_summary="",
            status="failure", latency_ms=0,
            error_code=code,
        )
    errs = store.failure_breakdown(tool_name="x")
    assert errs["timeout"] == 2
    assert errs["boom"] == 1


def test_record_persists_across_instances(tmp_path) -> None:
    """SQLite commits so a fresh store on the same DB sees prior rows."""
    db = os.path.join(str(tmp_path), "shared.sqlite3")
    s1 = PlatformToolCallLogStore(db_path=db)
    s1.record(
        run_id="r", tool_name="x",
        input_summary="", output_summary="",
        status="success", latency_ms=10, error_code=None,
    )
    s2 = PlatformToolCallLogStore(db_path=db)
    assert len(s2.list_for_run("r")) == 1


def test_tool_call_record_dataclass_round_trip() -> None:
    rec = ToolCallRecord(
        log_id=1, run_id="r", tool_name="x",
        input_summary="{}", output_summary="{}",
        status="success", latency_ms=10,
        error_code=None, created_at="2026-06-18T00:00:00Z",
    )
    d = rec.to_dict()
    assert d["tool_name"] == "x"
    assert d["latency_ms"] == 10


def test_record_without_run_id_is_allowed(store: PlatformToolCallLogStore) -> None:
    """Tool calls during template dry-run may not have a run_id yet."""
    store.record(
        run_id=None, tool_name="health",
        input_summary="", output_summary="",
        status="success", latency_ms=5, error_code=None,
    )
    rows = store.list_for_run(None)  # type: ignore[arg-type]
    assert len(rows) == 1


def test_p95_with_no_records_returns_zero(store: PlatformToolCallLogStore) -> None:
    assert store.latency_p95(tool_name="x") == 0.0
