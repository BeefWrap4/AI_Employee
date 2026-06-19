"""RCA report sections + tool_call_log tests (spec §6.4/§6.6)."""

from __future__ import annotations

from ai_employee.rca_agent.app import create_app
from ai_employee.rca_agent.runtime import RcaStore
from ai_employee.rca_agent.tool_call_log import RcaToolCallLogStore
from fastapi.testclient import TestClient


def _alarms(code: str = "LINK_DEGRADE"):
    return [
        {
            "alarm_id": "a_001",
            "alarm_code": code,
            "alarm_name": "Transmission link degradation",
            "vendor": "huawei",
            "site_id": "SITE-001",
            "cell_id": "CELL-001",
            "ne_id": "NE-001",
            "severity": "critical",
            "start_time": "2026-06-17T10:00:00+08:00",
            "raw_payload": {},
        },
        {
            "alarm_id": "a_002",
            "alarm_code": "RRC_SETUP_FAIL_HIGH",
            "alarm_name": "RRC setup failure rate high",
            "vendor": "huawei",
            "site_id": "SITE-001",
            "cell_id": "CELL-002",
            "ne_id": "NE-002",
            "severity": "major",
            "start_time": "2026-06-17T10:05:00+08:00",
            "raw_payload": {},
        },
    ]


def test_report_includes_impact_scope_timeline_and_sources() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": True,
            "alarms": _alarms(),
        },
    )
    assert resp.status_code == 201, resp.text
    report_id = resp.json()["report_id"]
    detail = client.get(f"/api/v1/rca/reports/{report_id}").json()
    md = detail["report_markdown"]
    # New sections from spec §6.6.
    assert "## 影响范围" in md
    assert "SITE-001" in md
    assert "NE-001" in md
    assert "NE-002" in md
    assert "## 关键时间线" in md
    assert "LINK_DEGRADE" in md  # alarm code appears in timeline
    assert "## 引用来源" in md
    # Old sections still present.
    assert "## 证据链" in md
    assert "## Top-N 根因候选" in md


def test_report_renders_contradicting_evidence_in_hypothesis_block() -> None:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": True,
            "alarms": _alarms(),
        },
    )
    report_id = resp.json()["report_id"]
    detail = client.get(f"/api/v1/rca/reports/{report_id}").json()
    md = detail["report_markdown"]
    # 反驳证据 label appears in the hypothesis block.
    assert "反驳证据" in md


def test_tool_call_log_records_per_adapter_call(tmp_path) -> None:
    """Verify that the end-to-end flow writes rows that the same
    RcaToolCallLogStore instance can read back.  This is a unit test
    that calls collect_evidence directly to avoid cross-process SQLite
    WAL quirks on Windows; the FastAPI path is covered by the e2e test
    in tests/platform-e2e/.
    """
    from ai_employee.rca_agent.runtime import build_incident, collect_evidence
    from ai_employee.rca_agent.schemas import RawAlarmEvent

    db = str(tmp_path / "calls.sqlite3")
    log_store = RcaToolCallLogStore(db_path=db)
    store = RcaStore(rca_tool_call_log=log_store)
    alarms = [RawAlarmEvent(**a) for a in _alarms()]
    for a in alarms:
        from ai_employee.rca_agent.runtime import normalize_alarm

        normalize_alarm(store, a)
    incident = build_incident(store, alarms)
    collect_evidence(
        incident,
        run_id="rca_run_test",
        tool_call_log=log_store,
    )
    rows = log_store.list_for_run("rca_run_test")
    assert len(rows) >= 4
    tool_names = {r["tool_name"] for r in rows}
    assert {"kpi", "log", "topology", "ticket"}.issubset(tool_names)


def test_e2e_run_records_in_tool_call_log(tmp_path) -> None:
    """FastAPI path: posting to /api/v1/rca/runs should call collect_evidence
    via the runtime path.  Marked xfail on Windows because the SQLite WAL
    visibility across the TestClient boundary is flaky there.
    """
    import sys

    if sys.platform.startswith("win"):
        import pytest

        pytest.skip("WAL visibility flaky on Windows; covered by direct unit test")
    log_store = RcaToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    store = RcaStore(rca_tool_call_log=log_store)
    client = TestClient(create_app(store=store))
    resp = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": False,
            "alarms": _alarms(),
        },
    )
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    rows = log_store.list_for_run(run_id)
    assert len(rows) >= 4
    """End-to-end: an RCA run should log every adapter fetch.

    On Windows + SQLite WAL the second connection opened by list_for_run
    can transiently see no rows before WAL is checkpointed; we mitigate
    by polling briefly (Windows file-locking quirk; same store + path).
    """
    import time as _t

    db = str(tmp_path / "calls.sqlite3")
    log_store = RcaToolCallLogStore(db_path=db)
    store = RcaStore(rca_tool_call_log=log_store)
    client = TestClient(create_app(store=store))
    resp = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 5,
            "require_human_review": False,
            "alarms": _alarms(),
        },
    )
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    # Poll for WAL visibility (best-effort up to 2s).
    rows: list = []
    for _ in range(20):
        rows = log_store.list_for_run(run_id)
        if rows:
            break
        _t.sleep(0.1)
    # 4 adapters (kpi/log/topology/ticket) should each have one entry.
    assert len(rows) >= 4, f"expected 4 rows, got {len(rows)}: {rows}"
    tool_names = {r["tool_name"] for r in rows}
    assert {"kpi", "log", "topology", "ticket"}.issubset(tool_names)
    assert all(r["status"] == "success" for r in rows)
    assert all(r["latency_ms"] >= 0 for r in rows)


def test_tool_call_log_success_rate(tmp_path) -> None:
    log_store = RcaToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    rate = log_store.success_rate(tool_name="kpi")
    assert rate == 1.0  # empty → 1.0 (no failures)
    log_store.record(
        run_id="r",
        tool_name="kpi",
        input_summary="x",
        output_summary="y",
        status="success",
        latency_ms=10,
        error_code=None,
    )
    assert log_store.success_rate(tool_name="kpi") == 1.0


# --------------------------------------------------------------------------- #
# R24-C: redaction in tool_call_log record()
# --------------------------------------------------------------------------- #


def test_rca_tool_call_log_redacts_input_summary(tmp_path) -> None:
    log_store = RcaToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    log_store.record(
        run_id="r1",
        tool_name="kpi",
        input_summary="phone 13800138000",
        output_summary="kpi value",
        status="success",
        latency_ms=10,
        error_code=None,
    )
    rows = log_store.list_for_run("r1")
    assert len(rows) == 1
    assert "13800138000" not in rows[0]["input_summary"]
    assert "***" in rows[0]["input_summary"]


def test_rca_tool_call_log_redacts_output_summary(tmp_path) -> None:
    log_store = RcaToolCallLogStore(db_path=str(tmp_path / "calls.sqlite3"))
    log_store.record(
        run_id="r1",
        tool_name="kpi",
        input_summary="kpi query",
        output_summary="contact admin@example.com",
        status="success",
        latency_ms=10,
        error_code=None,
    )
    rows = log_store.list_for_run("r1")
    assert len(rows) == 1
    assert "admin@example.com" not in rows[0]["output_summary"]
