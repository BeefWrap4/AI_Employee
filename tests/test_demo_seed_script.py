"""Structural checks for the local demo seed script."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed-demo.ps1"


def test_demo_seed_script_exists() -> None:
    assert SCRIPT.exists()


def test_demo_seed_script_creates_all_demo_data_types() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for token in (
        "/api/knowledge/api/v1/documents",
        "/api/knowledge/api/v1/chat/query",
        "tests/rca-replay/sample_cases.jsonl",
        "/api/rca/api/v1/rca/runs",
        "/api/platform/api/v1/agent-runs",
        "/api/platform/api/v1/tools",
        "/api/tools/api/v1/tools",
    ):
        assert token in text


def test_demo_seed_script_is_repeatable_and_emits_json_summary() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Idempotency-Key" in text
    assert "ConvertTo-Json" in text
    assert "-Json" in text
