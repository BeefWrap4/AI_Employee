"""CLI: python -m ai_employee.eval --golden ... --api ... --out ..."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from ai_employee.eval.golden import GoldenLoadError
from ai_employee.eval.metrics import compute
from ai_employee.eval.report import build_report, write_reports
from ai_employee.eval.runner import run


def _parse_top_ks(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_employee.eval",
        description="RAG offline evaluation: run golden QA set and emit report.",
    )
    parser.add_argument("--golden", required=True, help="path to golden.jsonl")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="knowledge-api base URL")
    parser.add_argument("--top-k", default="1,3,5", help="comma-separated K list (default 1,3,5)")
    parser.add_argument(
        "--out", default="var/data/eval_reports", help="output dir for report files"
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="per-query timeout (s)")
    parser.add_argument(
        "--threshold-top1", type=float, default=0.6, help="top-1 hit rate threshold"
    )
    parser.add_argument(
        "--threshold-top3", type=float, default=0.8, help="top-3 hit rate threshold"
    )
    parser.add_argument(
        "--threshold-refusal", type=float, default=0.9, help="refusal accuracy threshold"
    )
    args = parser.parse_args(argv)

    try:
        top_ks = _parse_top_ks(args.top_k)
    except ValueError as exc:
        print(f"invalid --top-k: {exc}", file=sys.stderr)
        return 2

    try:
        results = run(
            golden_path=args.golden,
            api_base=args.api,
            top_ks=top_ks,
            timeout=args.timeout,
        )
    except GoldenLoadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    metrics = compute(results, top_ks)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = build_report(
        metrics,
        golden_path=args.golden,
        api_base=args.api,
        top_ks=top_ks,
        ts=ts,
        thresholds={
            "top1": args.threshold_top1,
            "top3": args.threshold_top3,
            "refusal": args.threshold_refusal,
        },
    )
    json_path, md_path = write_reports(report, args.out)

    # stdout 摘要
    m = report["metrics"]
    summary = report["summary"]
    head = "✅" if report["pass"] else "❌"
    print(
        f"{head} RAG 评测: total={summary['total']} errored={summary['errored']} "
        f"refusal_violations={summary['refusal_violations']}"
    )
    for k in top_ks:
        print(
            f"  Top-{k} 命中: {m['hit_rates'][str(k)] * 100:.0f}% ({m['hit_counts'][str(k)]}/{m['eligible_for_hit']})"
        )
    if m["eligible_for_hit"]:
        print(f"  引用覆盖: {m['citation_coverage'] * 100:.0f}%")
    if summary.get("refusal_expected", 0) if False else m["refusal_expected"]:
        print(
            f"  拒答准确: {m['refusal_accuracy'] * 100:.0f}% ({m['refusal_correct']}/{m['refusal_expected']})"
        )
    print(f"  P95 延迟: {m['latency_p95_ms']:.0f} ms")
    print(f"  报表: {json_path}")
    print(f"       {md_path}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
