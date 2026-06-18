from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai_employee.rca_agent.runtime import RcaStore, run_rca
from ai_employee.rca_agent.schemas import RawAlarmEvent


def run_replay_file(path: str | Path) -> dict[str, Any]:
    cases = _load_cases(Path(path))
    results = [_run_case(case) for case in cases]
    total = len(results)
    top1_hits = sum(1 for item in results if item["top1_hit"])
    top3_hits = sum(1 for item in results if item["top3_hit"])
    evidence_coverages = [item["evidence_coverage"] for item in results]
    evidence_counts = [item["evidence_count"] for item in results]
    return {
        "total_cases": total,
        "top1_root_cause_coverage": _ratio(top1_hits, total),
        "top3_root_cause_coverage": _ratio(top3_hits, total),
        "evidence_coverage": _mean(evidence_coverages),
        "average_evidence_count": _mean(evidence_counts),
        "cases": results,
    }


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                cases.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
    if not cases:
        raise ValueError(f"replay file has no cases: {path}")
    return cases


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    store = RcaStore()
    alarms = [RawAlarmEvent(**alarm) for alarm in case["alarms"]]
    run = run_rca(
        store,
        raw_alarms=alarms,
        incident_id=None,
        require_human_review=True,
    )
    expected = case["expected_root_cause_type"]
    predicted = [hypothesis.root_cause_type for hypothesis in run.hypotheses]
    evidence_ids = {item.evidence_id for item in run.evidence}
    supporting_ids = {
        evidence_id
        for hypothesis in run.hypotheses
        for evidence_id in hypothesis.supporting_evidence_ids
    }
    covered_support = supporting_ids.intersection(evidence_ids)
    return {
        "case_id": case["case_id"],
        "expected_root_cause_type": expected,
        "predicted_root_cause_types": predicted,
        "top1_hit": bool(predicted and predicted[0] == expected),
        "top3_hit": expected in predicted[:3],
        "evidence_count": len(run.evidence),
        "evidence_coverage": _ratio(len(covered_support), len(supporting_ids)),
        "run_id": run.run_id,
        "report_id": run.report_id,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else round(numerator / denominator, 4)


def _mean(values: list[float | int]) -> float:
    return 0.0 if not values else round(sum(values) / len(values), 4)


def _format_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# RCA Replay Report",
        "",
        f"- Total cases: {report['total_cases']}",
        f"- Top-1 root-cause coverage: {report['top1_root_cause_coverage']:.2%}",
        f"- Top-3 root-cause coverage: {report['top3_root_cause_coverage']:.2%}",
        f"- Evidence coverage: {report['evidence_coverage']:.2%}",
        f"- Average evidence count: {report['average_evidence_count']:.2f}",
        "",
        "## Cases",
    ]
    for case in report["cases"]:
        lines.append(
            "- `{}` expected `{}` predicted `{}` top1={} top3={}".format(
                case["case_id"],
                case["expected_root_cause_type"],
                case["predicted_root_cause_types"][0],
                case["top1_hit"],
                case["top3_hit"],
            )
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RCA replay evaluation.")
    parser.add_argument("path", help="Path to RCA replay JSONL cases.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    args = parser.parse_args(argv)

    report = run_replay_file(args.path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
