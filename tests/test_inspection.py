"""Inspection agent unit tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_employee.agent_platform_api.inspection import (
    FixtureInspectionCheck,
    HttpHealthCheck,
    build_inspection_check,
    run_inspection,
    write_inspection_log,
)


def test_run_inspection_returns_findings_for_known_target() -> None:
    result = run_inspection("knowledge-api")
    assert result["target"] == "knowledge-api"
    assert result["risk_level"] == "read_only"
    assert isinstance(result["findings"], list)
    assert result["findings"]
    assert result["check_name"] == "fixture.inspect"
    assert result["summary"]["ok"] >= 1


def test_run_inspection_filters_by_check_items() -> None:
    result = run_inspection("knowledge-api", check_items=["service_health"])
    assert all(f["check_name"] == "service_health" for f in result["findings"])


def test_run_inspection_returns_empty_when_filter_excludes_all() -> None:
    result = run_inspection("knowledge-api", check_items=["nonexistent_check"])
    assert result["findings"] == []
    assert result["summary"] == {"ok": 0, "warning": 0, "error": 0}


def test_run_inspection_handles_unknown_target_via_fixture() -> None:
    result = run_inspection("unknown-svc-xyz")
    assert result["check_name"] == "fixture.inspect"
    assert any("fixture" in f["detail"].lower() for f in result["findings"])


def test_build_inspection_check_uses_http_when_env_set(monkeypatch) -> None:
    monkeypatch.setenv("INSPECT_KNOWLEDGE_API_URL", "http://k.local:8010")
    monkeypatch.setenv("INSPECT_KNOWLEDGE_API_URL_ENABLED", "true")
    check = build_inspection_check("knowledge-api")
    assert isinstance(check, HttpHealthCheck)


def test_http_check_reports_ok_on_200() -> None:
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "ok"
    with patch("httpx.get", return_value=fake):
        check = HttpHealthCheck("http://k.local:8010")
        findings = check.run("knowledge-api")
    assert findings[0]["severity"] == "ok"


def test_http_check_reports_error_on_500() -> None:
    fake = MagicMock()
    fake.status_code = 500
    fake.text = "boom"
    with patch("httpx.get", return_value=fake):
        check = HttpHealthCheck("http://k.local:8010")
        findings = check.run("knowledge-api")
    assert findings[0]["severity"] == "error"


def test_http_check_reports_error_on_network_failure() -> None:
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("nope")):
        check = HttpHealthCheck("http://does-not-exist.invalid:8010")
        findings = check.run("knowledge-api")
    assert findings[0]["severity"] == "error"
    assert "unreachable" in findings[0]["detail"]


def test_write_inspection_log_appends_jsonl(tmp_path) -> None:
    payload = run_inspection("knowledge-api")
    log_path = write_inspection_log(payload, log_dir=str(tmp_path))
    file_path = tmp_path / "knowledge-api.jsonl"
    assert str(file_path) == log_path
    contents = file_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 1
    import json as _json
    parsed = _json.loads(contents[0])
    assert parsed["target"] == "knowledge-api"
