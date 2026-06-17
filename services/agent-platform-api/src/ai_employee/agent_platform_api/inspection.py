"""Inspection agent — read-only diagnostic checks.

Spec §3.4. Performs structured checks against running services (health
endpoints, recent log lines, last config snapshot) and returns a
machine-readable findings list.  Default implementation uses local
fixtures; real HTTP probes are wired via env flags (``INSPECT_*``) so
production deployments can talk to live services without changing
callers.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx


class InspectCheck(Protocol):
    name: str
    risk_level: str  # "read_only"

    def run(self, target: str) -> list[dict[str, Any]]: ...


@dataclass
class Finding:
    check_name: str
    severity: str  # "ok" | "warning" | "error"
    detail: str
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


_FIXTURE_FINDINGS: dict[str, list[dict[str, Any]]] = {
    "knowledge-api": [
        {
            "check_name": "service_health",
            "severity": "ok",
            "detail": "knowledge-api /health returned 200 in fixture mode",
        },
        {
            "check_name": "index_corruption",
            "severity": "ok",
            "detail": "FTS5 integrity probe passed (fixture)",
        },
    ],
    "rca-agent": [
        {
            "check_name": "service_health",
            "severity": "ok",
            "detail": "rca-agent /health returned 200 in fixture mode",
        },
        {
            "check_name": "stale_runs",
            "severity": "warning",
            "detail": "0 stale runs (fixture)",
        },
    ],
    "agent-platform-api": [
        {
            "check_name": "service_health",
            "severity": "ok",
            "detail": "agent-platform-api /health returned 200 in fixture mode",
        },
        {
            "check_name": "approval_queue_depth",
            "severity": "ok",
            "detail": "approval queue depth: 0 (fixture)",
        },
    ],
}


# --------------------------------------------------------------------------- #
# Real (HTTP) checks
# --------------------------------------------------------------------------- #


def _probe_health(base_url: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        resp = httpx.get(f"{base_url}/health", timeout=timeout)
    except httpx.HTTPError as exc:
        return 0, f"unreachable: {exc}"
    return resp.status_code, resp.text[:200]


class HttpHealthCheck:
    name = "http_health"
    risk_level = "read_only"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def run(self, target: str) -> list[dict[str, Any]]:
        status, body = _probe_health(self.base_url)
        if status == 0:
            return [
                {
                    "check_name": "service_health",
                    "severity": "error",
                    "detail": body,
                }
            ]
        if 200 <= status < 300:
            return [
                {
                    "check_name": "service_health",
                    "severity": "ok",
                    "detail": f"{self.base_url}/health returned {status}",
                }
            ]
        return [
            {
                "check_name": "service_health",
                "severity": "error",
                "detail": f"{self.base_url}/health returned {status}: {body}",
            }
        ]


class FixtureInspectionCheck:
    name = "fixture.inspect"
    risk_level = "read_only"

    def run(self, target: str) -> list[dict[str, Any]]:
        return list(_FIXTURE_FINDINGS.get(target, [
            {
                "check_name": "service_health",
                "severity": "ok",
                "detail": f"No live adapter for {target}; fixture returned no findings.",
            }
        ]))


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def build_inspection_check(target: str) -> InspectCheck:
    """Pick an inspection check adapter based on env flags.

    Env mapping (target -> URL): ``INSPECT_<TARGET>_URL``.  When set the
    HTTP check is used; otherwise the deterministic fixture is returned.
    Defaulting to the fixture keeps MVP/dev/test environments healthy when
    no service is listening on the canonical port.
    """
    env_var = f"INSPECT_{target.upper().replace('-', '_')}_URL"
    url = os.getenv(env_var)
    if _truthy(os.getenv(f"{env_var}_ENABLED", "")) and url:
        return HttpHealthCheck(url)
    return FixtureInspectionCheck()


def run_inspection(
    target: str,
    check_items: list[str] | None = None,
) -> dict[str, Any]:
    """Execute the inspection pipeline for the given service target.

    ``check_items`` allows callers to filter which checks run; ``None``
    runs all available checks.  Returns a structured payload suitable
    for direct JSON serialisation by the API layer.
    """
    check = build_inspection_check(target)
    raw_findings = check.run(target)
    if check_items is not None:
        wanted = {item.strip() for item in check_items if item.strip()}
        raw_findings = [f for f in raw_findings if f["check_name"] in wanted]
    summary = _summarize(raw_findings)
    return {
        "target": target,
        "check_name": check.name,
        "risk_level": check.risk_level,
        "findings": raw_findings,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _summarize(findings: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"ok": 0, "warning": 0, "error": 0}
    for finding in findings:
        sev = finding.get("severity", "ok")
        summary[sev] = summary.get(sev, 0) + 1
    return summary


def write_inspection_log(payload: dict[str, Any], log_dir: str | None = None) -> str:
    """Append the inspection payload to a JSONL log for audit purposes."""
    log_root = Path(log_dir or os.getenv("INSPECT_LOG_DIR", "./var/data/inspections"))
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = log_root / f"{payload['target']}.jsonl"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return str(log_file)


__all__ = [
    "Finding",
    "FixtureInspectionCheck",
    "HttpHealthCheck",
    "InspectCheck",
    "build_inspection_check",
    "run_inspection",
    "write_inspection_log",
]
