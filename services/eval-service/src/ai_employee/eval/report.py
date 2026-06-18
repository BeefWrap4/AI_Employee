from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ai_employee.eval.metrics import EvalMetrics


def report_filename(ts: datetime, ext: str = "json") -> str:
    return f"report_{ts.strftime('%Y%m%d-%H%M%S')}.{ext}"


def build_report(
    metrics: EvalMetrics,
    *,
    golden_path: str,
    api_base: str,
    top_ks: list[int],
    ts: str,
    thresholds: dict[str, float],
) -> dict:
    """构建报表 dict。pass 条件：所有阈值满足 + 无 refusal_violation。"""
    pass_ = (
        metrics.refusal_violations == 0
        and all(metrics.hit_rates.get(k, 0) >= thresholds.get(f"top{k}", 0) for k in top_ks)
        and metrics.refusal_accuracy >= thresholds.get("refusal", 0)
    )
    return {
        "ts": ts,
        "golden_path": golden_path,
        "api_base": api_base,
        "top_ks": list(top_ks),
        "summary": {
            "total": metrics.total,
            "errored": metrics.errored,
            "refusal_violations": metrics.refusal_violations,
        },
        "metrics": {
            "hit_rates": {str(k): v for k, v in metrics.hit_rates.items()},
            "hit_counts": {str(k): v for k, v in metrics.hit_counts.items()},
            "eligible_for_hit": metrics.eligible_for_hit,
            "citation_coverage": metrics.citation_coverage,
            "refusal_expected": metrics.refusal_expected,
            "refusal_correct": metrics.refusal_correct,
            "refusal_accuracy": metrics.refusal_accuracy,
            "latency_p50_ms": metrics.latency_p50_ms,
            "latency_p95_ms": metrics.latency_p95_ms,
            "latency_mean_ms": metrics.latency_mean_ms,
        },
        "thresholds": dict(thresholds),
        "pass": pass_,
        "per_item": list(metrics.per_item),
    }


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def _emoji(value: float, threshold: float) -> str:
    return "✅" if value >= threshold else "❌"


def render_markdown(report: dict) -> str:
    m = report["metrics"]
    t = report["thresholds"]
    pass_ = report["pass"]
    summary = report["summary"]
    head = "✅" if pass_ else "❌"

    lines: list[str] = []
    lines.append(f"# RAG 评测报告 — {report['ts']}")
    lines.append("")
    lines.append(f"- API: {report['api_base']}")
    lines.append(f"- 黄金集: {report['golden_path']}（{summary['total']} 条）")
    lines.append(f"- 结果: {head} {'PASS' if pass_ else 'FAIL'}")
    lines.append("")
    lines.append("## 指标")
    lines.append("")
    lines.append("| 指标 | 值 | 阈值 | 状态 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| Total | {summary['total']} | — | — |")
    lines.append(f"| Errored | {summary['errored']} | — | — |")
    lines.append(f"| 拒答误判 | {summary['refusal_violations']} | — | — |")
    for k in report["top_ks"]:
        ks = str(k)
        rate = m["hit_rates"].get(ks, 0)
        cnt = m["hit_counts"].get(ks, 0)
        thr = t.get(f"top{k}", 0)
        lines.append(
            f"| Top-{k} 命中 | {rate * 100:.0f}% ({cnt}/{m['eligible_for_hit']}) | ≥ {thr * 100:.0f}% | "
            f"{_emoji(rate, thr)} |"
        )
    if m["eligible_for_hit"]:
        cov = m["citation_coverage"]
        covered = round(cov * m["eligible_for_hit"])
        lines.append(f"| 引用覆盖 | {cov * 100:.0f}% ({covered}/{m['eligible_for_hit']}) | — | — |")
    else:
        lines.append("| 引用覆盖 | — | — | — |")
    re_ = m["refusal_expected"]
    if re_:
        rc = m["refusal_correct"]
        acc = m["refusal_accuracy"]
        thr = t.get("refusal", 0)
        lines.append(
            f"| 拒答准确 | {acc * 100:.0f}% ({rc}/{re_}) | ≥ {thr * 100:.0f}% | {_emoji(acc, thr)} |"
        )
    else:
        lines.append("| 拒答准确 | — | — | — |")
    lines.append(f"| P50 延迟 | {m['latency_p50_ms']:.0f} ms | — | — |")
    lines.append(f"| P95 延迟 | {m['latency_p95_ms']:.0f} ms | — | — |")
    lines.append(f"| 平均延迟 | {m['latency_mean_ms']:.0f} ms | — | — |")
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    lines.append("| qid | 判定 | expected | 返回 | latency |")
    lines.append("|---|---|---|---|---|")
    for item in report["per_item"]:
        qid = item["qid"]
        verdict = item.get("verdict", "")
        expected = item.get("expected") or "—"
        returned = ", ".join(item.get("returned", [])) or "—"
        latency = item.get("latency_ms", 0)
        lines.append(f"| {qid} | {verdict} | {expected} | {returned} | {latency} ms |")
    lines.append("")
    return "\n".join(lines)


def write_reports(report: dict, out_dir: str) -> tuple[str, str]:
    """写入 report_{ts}.json + report_{ts}.md，返回两条路径。"""
    os.makedirs(out_dir, exist_ok=True)
    dt = datetime.strptime(report["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    stamp = dt.strftime("%Y%m%d-%H%M%S")
    json_path = Path(out_dir) / f"report_{stamp}.json"
    md_path = Path(out_dir) / f"report_{stamp}.md"
    json_path.write_text(render_json(report), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return str(json_path), str(md_path)


__all__ = ["build_report", "render_json", "render_markdown", "report_filename", "write_reports"]
