import pytest

from ai_employee.eval.metrics import (
    EvalMetrics,
    EvalResult,
    compute,
    _percentile,
)


def _r(qid, *, expect_refusal=False, expected_doc_id=None, status_code=200,
       returned=None, latency=100, error=None, answer="A"):
    return EvalResult(
        qid=qid, question="x", expected_doc_id=expected_doc_id,
        expect_refusal=expect_refusal, status_code=status_code,
        returned_doc_ids=returned or [], answer=answer, latency_ms=latency, error=error,
    )


def test_compute_top_k_hit_rates() -> None:
    results = [
        _r("q01", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d1", "d2"]),
        _r("q02", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d2", "d1", "d3"]),
        _r("q03", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d2", "d3", "d4", "d5", "d1"]),
        _r("q04", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d2"]),
    ]
    m = compute(results, top_ks=[1, 3, 5])
    assert m.hit_rates == {1: 0.5, 3: 0.75, 5: 1.0}
    assert m.hit_counts == {1: 2, 3: 3, 5: 4}
    assert m.eligible_for_hit == 4


def test_compute_refusal_accuracy() -> None:
    results = [
        _r("q01", expect_refusal=True, status_code=404),
        _r("q02", expect_refusal=True, status_code=404),
        _r("q03", expect_refusal=True, status_code=200),
        _r("q04", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d1"]),
    ]
    m = compute(results, top_ks=[1])
    assert m.refusal_expected == 3
    assert m.refusal_correct == 2
    assert m.refusal_accuracy == 2 / 3
    assert m.refusal_violations == 1


def test_compute_http_error_does_not_count() -> None:
    results = [
        _r("q01", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d1"]),
        _r("q02", expect_refusal=False, expected_doc_id="d1", status_code=0, error="timeout"),
        _r("q03", expect_refusal=True, status_code=0, error="conn refused"),
    ]
    m = compute(results, top_ks=[1])
    assert m.total == 3
    assert m.errored == 2
    assert m.hit_rates[1] == 1.0
    assert m.refusal_accuracy == 0.0


def test_compute_citation_coverage() -> None:
    results = [
        _r("q01", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d1"]),
        _r("q02", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=[]),
        _r("q03", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d2"]),
        _r("q04", expect_refusal=True, status_code=404),
    ]
    m = compute(results, top_ks=[1])
    assert m.citation_coverage == 1 / 3


def test_compute_latency_percentiles() -> None:
    results = [_r(f"q{i:02d}", latency_ms=(i + 1) * 10) for i in range(100)]
    m = compute(results, top_ks=[1])
    assert m.latency_p50_ms == 550.0
    assert m.latency_p95_ms == 955.0
    assert m.latency_mean_ms == 505.0


def test_percentile_basic() -> None:
    assert _percentile([1, 2, 3, 4, 5], 50) == 3
    assert _percentile([1, 2, 3, 4, 5], 100) == 5
    assert _percentile([5], 50) == 5
    assert _percentile([], 50) == 0.0


def test_compute_per_item_verdicts() -> None:
    results = [
        _r("q01", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d1"]),
        _r("q02", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d2", "d1"]),
        _r("q03", expect_refusal=False, expected_doc_id="d1", status_code=200, returned=["d2"]),
        _r("q04", expect_refusal=True, status_code=404),
        _r("q05", expect_refusal=True, status_code=200),
        _r("q06", error="boom"),
    ]
    m = compute(results, top_ks=[1, 3])
    by_qid = {p["qid"]: p["verdict"] for p in m.per_item}
    assert by_qid["q01"] == "hit@1"
    assert by_qid["q02"] == "hit@3"
    assert by_qid["q03"] == "miss"
    assert by_qid["q04"] == "refusal"
    assert by_qid["q05"] == "refusal_violation"
    assert by_qid["q06"] == "error"


def test_compute_zero_eligible() -> None:
    results = [
        _r("q01", expect_refusal=True, status_code=404),
        _r("q02", error="x"),
    ]
    m = compute(results, top_ks=[1])
    assert m.eligible_for_hit == 0
    assert m.hit_rates[1] == 0.0
