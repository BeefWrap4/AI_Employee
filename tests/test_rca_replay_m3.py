from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ai_employee.rca_agent.replay import run_replay_file


SAMPLE_CASES = Path("tests/rca-replay/sample_cases.jsonl")


def test_run_replay_file_outputs_root_cause_and_evidence_metrics() -> None:
    report = run_replay_file(SAMPLE_CASES)

    assert report["total_cases"] == 2
    assert report["top1_root_cause_coverage"] == 1.0
    assert report["top3_root_cause_coverage"] == 1.0
    assert report["evidence_coverage"] == 1.0
    assert report["average_evidence_count"] == 5.0
    assert [case["case_id"] for case in report["cases"]] == [
        "case_transport_link_001",
        "case_wireless_access_001",
    ]
    assert report["cases"][0]["predicted_root_cause_types"][0] == "transmission_link_degradation"
    assert report["cases"][1]["predicted_root_cause_types"][0] == "wireless_access_anomaly"


def test_rca_replay_cli_emits_json_report() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_employee.rca_agent.replay",
            str(SAMPLE_CASES),
            "--json",
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["total_cases"] == 2
    assert body["top1_root_cause_coverage"] == 1.0
