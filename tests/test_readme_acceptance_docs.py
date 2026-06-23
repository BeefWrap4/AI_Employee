"""Checks for operator-facing runbooks and acceptance documentation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ACCEPTANCE = ROOT / "Docs" / "mvp-acceptance-checklist.md"


def test_readme_documents_one_click_docker_demo() -> None:
    text = README.read_text(encoding="utf-8")
    for token in (
        "Docker Compose 一键演示",
        "scripts\\docker-smoke.ps1",
        "scripts\\seed-demo.ps1",
        "http://127.0.0.1:5173",
        "npm run e2e:docker",
    ):
        assert token in text


def test_acceptance_checklist_covers_demo_api_and_verification() -> None:
    text = ACCEPTANCE.read_text(encoding="utf-8")
    for token in (
        "MVP 端到端验收清单",
        "知识问答",
        "RCA 诊断",
        "Agent 平台运行记录",
        "API 健康检查",
        "docker-smoke.ps1",
        "seed-demo.ps1",
        "npm run e2e:docker",
    ):
        assert token in text
