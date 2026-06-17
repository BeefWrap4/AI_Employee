import json
from datetime import datetime, timezone

import pytest

from ai_employee.eval.metrics import EvalMetrics, EvalResult, compute
from ai_employee.eval.report import build_report, render_json, render_markdown


def _sample_results():
    return [
        EvalResult("q01", "Q1", "d1", False, 200, ["d1"], "A1", 110, None),
        EvalResult("q02", "Q2", "d1", False, 200, ["d2"], "A2", 220, None),
        EvalResult("q03", "Q3", "d1", True, 404, [], "", 25, None),
    ]


def test_build_report_returns_dict_with_required_keys() -> None:
    metrics = compute(_sample_results(), top_ks=[1, 3])
    rpt = build_report(
        metrics=metrics,
        golden_path="golden.jsonl",
        api_base="http://api",
        top_ks=[1, 3],
        ts="2026-06-17T12:00:00Z",
        thresholds={"top1": 0.6, "top3": 0.8, "refusal": 0.9},
    )
    assert rpt["ts"] == "2026-06-17T12:00:00Z"
    assert rpt["golden_path"] == "golden.jsonl"
    assert rpt["top_ks"] == [1, 3]
    assert "summary" in rpt
    assert "metrics" in rpt
    assert "thresholds" in rpt
    assert "pass" in rpt
    assert "per_item" in rpt


def test_build_report_pass_under_thresholds() -> None:
    metrics = compute(_sample_results(), top_ks=[1])
    rpt = build_report(
        metrics=metrics, golden_path="g", api_base="a", top_ks=[1],
        ts="t", thresholds={"top1": 0.0, "top3": 0.0, "refusal": 0.0},
    )
    assert rpt["pass"] is True


def test_build_report_fail_when_below_threshold() -> None:
    metrics = compute(_sample_results(), top_ks=[1])
    rpt = build_report(
        metrics=metrics, golden_path="g", api_base="a", top_ks=[1],
        ts="t", thresholds={"top1": 0.99, "top3": 0.99, "refusal": 0.0},
    )
    assert rpt["pass"] is False


def test_build_report_fail_on_refusal_violation() -> None:
    # 有应拒未拒（refusal_violations>0）即使其他指标都高也 FAIL
    metrics = EvalMetrics(
        total=2, errored=0, refusal_violations=1, eligible_for_hit=1,
        hit_counts={1: 1, 3: 1}, hit_rates={1: 1.0, 3: 1.0},
        citation_coverage=1.0, refusal_expected=1, refusal_correct=0,
        refusal_accuracy=0.0, latency_p50_ms=10, latency_p95_ms=10, latency_mean_ms=10,
        per_item=[],
    )
    rpt = build_report(
        metrics=metrics, golden_path="g", api_base="a", top_ks=[1],
        ts="t", thresholds={"top1": 0.0, "top3": 0.0, "refusal": 0.0},
    )
    assert rpt["pass"] is False


def test_render_json_is_valid_json() -> None:
    metrics = compute(_sample_results(), top_ks=[1])
    rpt = build_report(metrics, "g", "a", [1], "t", {"top1": 0, "top3": 0, "refusal": 0})
    s = render_json(rpt)
    parsed = json.loads(s)
    assert parsed["ts"] == "t"


def test_render_markdown_contains_table() -> None:
    metrics = compute(_sample_results(), top_ks=[1, 3])
    rpt = build_report(
        metrics, "g", "a", [1, 3], "t",
        thresholds={"top1": 0.6, "top3": 0.8, "refusal": 0.9},
    )
    md = render_markdown(rpt)
    assert "# RAG 评测报告" in md
    assert "## 指标" in md
    assert "## 明细" in md
    assert "Top-1 命中" in md
    assert "q01" in md


def test_render_markdown_emoji_under_threshold() -> None:
    metrics = compute(_sample_results(), top_ks=[1])
    rpt = build_report(
        metrics, "g", "a", [1], "t",
        thresholds={"top1": 0.99, "top3": 0.99, "refusal": 0.0},
    )
    md = render_markdown(rpt)
    assert "❌" in md
