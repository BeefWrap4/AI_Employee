from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class EvalResult:
    qid: str
    question: str
    expected_doc_id: str | None
    expect_refusal: bool
    status_code: int
    returned_doc_ids: list[str]
    answer: str
    latency_ms: int
    error: str | None = None


@dataclass
class EvalMetrics:
    total: int = 0
    errored: int = 0
    refusal_violations: int = 0
    eligible_for_hit: int = 0
    hit_counts: dict[int, int] = field(default_factory=dict)
    hit_rates: dict[int, float] = field(default_factory=dict)
    citation_coverage: float = 0.0
    refusal_expected: int = 0
    refusal_correct: int = 0
    refusal_accuracy: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_mean_ms: float = 0.0
    per_item: list[dict] = field(default_factory=list)


def _percentile(values: list[float], p: float) -> float:
    """线性插值分位数（0<=p<=100）。空列表返回 0.0。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    if p <= 0:
        return float(s[0])
    if p >= 100:
        return float(s[-1])
    rank = (p / 100) * (len(s) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(s[lo])
    frac = rank - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


def _hit_verdict(returned: list[str], expected: str, top_ks: list[int]) -> str:
    if not returned or expected not in returned:
        return "miss"
    for k in top_ks:
        if expected in returned[:k]:
            return f"hit@{k}"
    return "miss"


def compute(results: list[EvalResult], top_ks: list[int]) -> EvalMetrics:
    m = EvalMetrics(total=len(results))
    for k in top_ks:
        m.hit_counts[k] = 0
        m.hit_rates[k] = 0.0

    hit_eligible: list[EvalResult] = []
    refusal_expected: list[EvalResult] = []
    covered = 0
    latencies: list[int] = []

    for r in results:
        latencies.append(r.latency_ms)
        if r.error or r.status_code == 0:
            m.errored += 1
            m.per_item.append(
                {"qid": r.qid, "verdict": "error", "expected": r.expected_doc_id,
                 "returned": [], "latency_ms": r.latency_ms, "error": r.error}
            )
            continue

        if r.expect_refusal:
            refusal_expected.append(r)
            if r.status_code == 404:
                m.refusal_correct += 1
                m.per_item.append(
                    {"qid": r.qid, "verdict": "refusal", "expected": None,
                     "returned": [], "status_code": 404, "latency_ms": r.latency_ms}
                )
            else:
                m.refusal_violations += 1
                m.per_item.append(
                    {"qid": r.qid, "verdict": "refusal_violation", "expected": None,
                     "returned": r.returned_doc_ids, "status_code": r.status_code,
                     "latency_ms": r.latency_ms}
                )
            continue

        hit_eligible.append(r)
        verdict = _hit_verdict(r.returned_doc_ids, r.expected_doc_id or "", top_ks)
        for k in top_ks:
            if r.expected_doc_id and r.expected_doc_id in r.returned_doc_ids[:k]:
                m.hit_counts[k] += 1
        if r.expected_doc_id and r.returned_doc_ids and r.expected_doc_id in r.returned_doc_ids:
            covered += 1
        m.per_item.append(
            {"qid": r.qid, "verdict": verdict, "expected": r.expected_doc_id,
             "returned": r.returned_doc_ids, "latency_ms": r.latency_ms}
        )

    m.eligible_for_hit = len(hit_eligible)
    for k in top_ks:
        m.hit_rates[k] = m.hit_counts[k] / m.eligible_for_hit if m.eligible_for_hit else 0.0
    m.refusal_expected = len(refusal_expected)
    m.refusal_accuracy = m.refusal_correct / m.refusal_expected if m.refusal_expected else 0.0
    m.citation_coverage = covered / m.eligible_for_hit if m.eligible_for_hit else 0.0

    if latencies:
        m.latency_p50_ms = _percentile([float(x) for x in latencies], 50)
        m.latency_p95_ms = _percentile([float(x) for x in latencies], 95)
        m.latency_mean_ms = sum(latencies) / len(latencies)
    return m


__all__ = ["EvalResult", "EvalMetrics", "compute", "_percentile"]
