"""Tool governance: tool_call_log, health check, timeout, circuit breaker."""
from __future__ import annotations

import time

import pytest
from ai_employee.common_schemas.tool_registry import ToolSpec
from ai_employee.tool_registry.circuit_breaker import CircuitBreaker, CircuitOpenError
from ai_employee.tool_registry.tool_call_log import ToolCallLogStore

# --- ToolSpec governance fields ----------------------------------------- #


def test_toolspec_defaults_governance_fields() -> None:
    spec = ToolSpec(name="x", description="d", input_schema={}, output_schema={})
    assert spec.timeout_ms == 5000
    assert spec.retry_policy == {"max_retries": 0}
    assert spec.health_check_url is None


def test_toolspec_to_mcp_includes_governance() -> None:
    spec = ToolSpec(
        name="x", description="d", input_schema={}, output_schema={},
        timeout_ms=2000, retry_policy={"max_retries": 2},
        health_check_url="http://x/health",
    )
    meta = spec.to_mcp_tool()["metadata"]
    assert meta["timeout_ms"] == 2000
    assert meta["retry_policy"] == {"max_retries": 2}
    assert meta["health_check_url"] == "http://x/health"


# --- tool_call_log ------------------------------------------------------ #


def test_tool_call_log_records_invocation(tmp_path) -> None:
    store = ToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    log_id = store.record(
        run_id="run_001", tool_name="echo",
        input={"text": "hi"}, output_summary='{"echo":"hi"}',
        status="success", latency_ms=12, error_code=None,
    )
    assert log_id.startswith("tcl_")
    rows = store.list_for_run("run_001")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "echo"
    assert rows[0]["status"] == "success"
    assert rows[0]["latency_ms"] == 12


def test_tool_call_log_records_failure(tmp_path) -> None:
    store = ToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    store.record(
        run_id="run_002", tool_name="boom",
        input={}, output_summary="",
        status="failed", latency_ms=50, error_code="TIMEOUT",
    )
    rows = store.list_for_run("run_002")
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_code"] == "TIMEOUT"


def test_tool_call_log_success_rate(tmp_path) -> None:
    store = ToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    store.record(run_id="r", tool_name="t", input={}, output_summary="", status="success", latency_ms=10, error_code=None)
    store.record(run_id="r", tool_name="t", input={}, output_summary="", status="success", latency_ms=20, error_code=None)
    store.record(run_id="r", tool_name="t", input={}, output_summary="", status="failed", latency_ms=30, error_code="ERR")
    rate = store.success_rate(tool_name="t")
    assert rate == pytest.approx(2 / 3)


def test_tool_call_log_paginates(tmp_path) -> None:
    store = ToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    for i in range(5):
        store.record(run_id="r", tool_name="t", input={}, output_summary="", status="success", latency_ms=i, error_code=None)
    rows, total = store.list(page=1, page_size=2)
    assert total == 5
    assert len(rows) == 2


# --- circuit breaker ---------------------------------------------------- #


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=60)

    def always_fail() -> dict:
        raise RuntimeError("boom")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call("tool", always_fail)
    assert cb.state("tool") == "open"
    with pytest.raises(CircuitOpenError):
        cb.call("tool", always_fail)


def test_circuit_breaker_stays_closed_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=60)

    def ok() -> dict:
        return {"ok": True}

    result = cb.call("tool", ok)
    assert result == {"ok": True}
    assert cb.state("tool") == "closed"


def test_circuit_breaker_half_open_after_recovery(monkeypatch) -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_seconds=1)
    call_count = {"n": 0}

    def flaky() -> dict:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise RuntimeError("boom")
        return {"ok": True}

    # Trigger open.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call("tool", flaky)
    assert cb.state("tool") == "open"

    # Move time forward past recovery window.
    monkeypatch.setattr(cb, "_now", lambda: time.time() + 5)
    # Next call enters half-open and succeeds → closes.
    result = cb.call("tool", flaky)
    assert result == {"ok": True}
    assert cb.state("tool") == "closed"


def test_circuit_breaker_resets_failure_count_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=60)

    def ok() -> dict:
        return {"ok": True}

    def fail() -> dict:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        cb.call("tool", fail)
    with pytest.raises(RuntimeError):
        cb.call("tool", fail)
    # Success resets the consecutive-failure counter.
    cb.call("tool", ok)
    assert cb.state("tool") == "closed"
    # Two more failures should NOT open (counter reset).
    with pytest.raises(RuntimeError):
        cb.call("tool", fail)
    with pytest.raises(RuntimeError):
        cb.call("tool", fail)
    assert cb.state("tool") == "closed"
