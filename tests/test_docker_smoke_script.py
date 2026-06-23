"""Structural checks for the local Docker smoke script."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "docker-smoke.ps1"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_docker_smoke_script_exists() -> None:
    assert SCRIPT.exists()


def test_docker_smoke_script_starts_compose_and_waits_for_health() -> None:
    text = _script_text()
    assert "docker compose" in text
    assert "up -d --build" in text
    assert "Wait-ComposeServicesHealthy" in text
    assert "docker compose" in text and "ps" in text


def test_docker_smoke_script_verifies_core_http_endpoints() -> None:
    text = _script_text()
    for path in (
        "http://127.0.0.1:5173/",
        "/api/knowledge/health",
        "/api/rca/health",
        "/api/platform/health",
        "/api/tools/health",
    ):
        assert path in text


def test_docker_smoke_script_records_each_http_check() -> None:
    text = _script_text()
    assert "$httpChecks += Assert-HttpOk" in text


def test_docker_smoke_script_exercises_rag_and_rca_flows() -> None:
    text = _script_text()
    for token in (
        "/api/knowledge/api/v1/documents",
        "/publish",
        "/api/knowledge/api/v1/chat/query",
        "tests/rca-replay/sample_cases.jsonl",
        "/api/rca/api/v1/rca/runs",
    ):
        assert token in text


def test_docker_smoke_script_emits_json_summary() -> None:
    text = _script_text()
    assert "ConvertTo-Json" in text
    assert "-Json" in text
