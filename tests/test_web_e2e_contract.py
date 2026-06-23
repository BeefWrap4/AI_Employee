"""Contract checks for the web portal E2E smoke suite."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web-portal"
PACKAGE_JSON = WEB / "package.json"
SPEC = WEB / "e2e" / "docker-smoke.spec.js"
CONFIG = WEB / "playwright.config.js"


def test_web_portal_declares_e2e_scripts_and_dependency() -> None:
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    assert package["scripts"]["e2e"] == "playwright test"
    assert package["scripts"]["e2e:docker"] == "playwright test e2e/docker-smoke.spec.js"
    assert "@playwright/test" in package["devDependencies"]


def test_web_portal_e2e_spec_covers_dashboard_knowledge_and_rca() -> None:
    text = SPEC.read_text(encoding="utf-8")
    for token in (
        "平台总览",
        "演示流程",
        "知识库",
        "检索问答",
        "引用证据",
        "RCA 诊断",
        "查看报告",
        "RCA 报告",
        "查看运行记录",
        "最近运行记录",
        "/api/platform/api/v1/agent-runs",
        "/trace",
    ):
        assert token in text


def test_web_portal_playwright_config_uses_base_url_env() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    assert "E2E_BASE_URL" in text
    assert "http://127.0.0.1:5173" in text
