from __future__ import annotations

import json
import subprocess
import sys


def test_m1_smoke_script_runs_local_upload_query_feedback_flow(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/m1_smoke.py",
            "--data-dir",
            str(tmp_path / "data"),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["document"]["parse_status"] == "published"
    assert payload["document"]["chunk_count"] >= 1
    assert payload["query"]["trace_id"].startswith("trace_")
    assert payload["query"]["citation_count"] >= 1
    assert payload["feedback"]["feedback_type"] == "useful"
    assert payload["audit"]["qa_log_total"] == 1
    assert payload["audit"]["feedback_total"] == 1
