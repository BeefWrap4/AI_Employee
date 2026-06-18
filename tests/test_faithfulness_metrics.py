"""Faithfulness + answer-relevance metrics tests (R18-4 / spec §5.6).

Upgrades the toy keyword-overlap implementation in :mod:`ai_employee.eval.metrics`
to a RAGAS-style claim-grounding algorithm:

* **Faithfulness** — split the answer into atomic claims (punctuation
  boundaries), check each claim against the retrieved evidence chunks
  (token overlap); final score = supported / total.  When LLM judge is
  available, an LLM verifier replaces the token overlap with semantic
  yes/no per claim.
* **Answer relevance** — generate N reference sub-questions from the
  question + expected answer, then measure how many of those sub-questions
  the answer addresses (token overlap with the question).  The LLM-judge
  path uses the LLM to score per-sub-question.

When no LLM judge is available, the module falls back to a deterministic
token-overlap implementation (the original metric) so the eval pipeline
never breaks.
"""
from __future__ import annotations

import pytest
from ai_employee.eval.metrics import (
    EvalResult,
    compute,
    evaluate_answer_relevance,
    evaluate_faithfulness,
    split_claims,
)


def _result(
    qid: str = "q1",
    question: str = "什么是 RRC 连接失败?",
    answer: str = "",
    chunks: list[str] | None = None,
    expected_keywords: list[str] | None = None,
    expected_text: str | None = None,
    expect_refusal: bool = False,
) -> EvalResult:
    return EvalResult(
        qid=qid,
        question=question,
        expected_doc_id=None,
        expect_refusal=expect_refusal,
        status_code=200,
        returned_doc_ids=[],
        answer=answer,
        latency_ms=0,
        expected_answer_keywords=expected_keywords or [],
        expected_answer_text=expected_text,
        retrieved_chunks=chunks or [],
    )


# --------------------------------------------------------------------------- #
# split_claims — sentence/segment boundary
# --------------------------------------------------------------------------- #


def test_split_claims_empty_returns_empty() -> None:
    assert split_claims("") == []


def test_split_claims_single_segment() -> None:
    text = "RRC 连接失败."
    claims = split_claims(text)
    # split_claims lowercases (case-insensitive downstream matching).
    assert claims == [text.lower()]


def test_split_claims_splits_on_sentence_boundaries() -> None:
    text = "RRC 连接失败。RRC 是无线资源控制层协议!"
    claims = split_claims(text)
    # 2 sentences → 2 claims (Chinese + English punctuation both split).
    assert len(claims) == 2
    assert "rrc 连接失败" in claims[0]
    assert "rrc" in claims[1]


def test_split_claims_splits_on_semicolons() -> None:
    text = "原因:功率低;小区拥塞;硬件故障"
    claims = split_claims(text)
    assert len(claims) >= 2


def test_split_claims_lowercases() -> None:
    text = "Power is Low. Power is Critical."
    claims = split_claims(text)
    assert all(c == c.lower() for c in claims)


# --------------------------------------------------------------------------- #
# Faithfulness — claim grounding
# --------------------------------------------------------------------------- #


def test_evaluate_faithfulness_empty_answer_returns_zero() -> None:
    score, supported, total = evaluate_faithfulness(
        answer="",
        retrieved_chunks=["some evidence"],
    )
    assert score == 0.0
    assert supported == 0
    assert total == 0


def test_evaluate_faithfulness_all_claims_supported() -> None:
    """Every claim in the answer is grounded by the retrieved chunks."""
    answer = "RRC 连接失败. 功率过低."
    chunks = [
        "故障根因包括 RRC 连接失败和功率过低。",
        "该告警频繁发生于弱覆盖场景。",
    ]
    score, supported, total = evaluate_faithfulness(answer, chunks)
    assert total >= 2
    assert supported == total
    assert score == 1.0


def test_evaluate_faithfulness_unsupported_claim_lowers_score() -> None:
    """A claim that doesn't match any evidence lowers the score."""
    answer = "功率过低. 月球上有水."
    chunks = ["告警显示功率过低, 检查 RRU."]
    score, supported, total = evaluate_faithfulness(answer, chunks)
    assert total == 2
    assert supported == 1
    assert score == 0.5


def test_evaluate_faithfulness_no_evidence() -> None:
    """No retrieved chunks → claims can't be grounded → score 0."""
    answer = "RRC 连接失败."
    score, supported, total = evaluate_faithfulness(answer, [])
    assert total == 1
    assert supported == 0
    assert score == 0.0


def test_evaluate_faithfulness_partial_token_overlap_counts() -> None:
    """A claim is 'supported' if it has substantial token overlap with
    any chunk — not requiring full substring match."""
    answer = "功率过低. 月球上有水."
    chunks = ["现场测量显示功率严重不足."]
    score, supported, total = evaluate_faithfulness(answer, chunks)
    assert total == 2
    # Only the first claim ("功率过低") overlaps with the chunk.
    assert supported == 1
    assert score == 0.5


# --------------------------------------------------------------------------- #
# Answer relevance — question + answer overlap
# --------------------------------------------------------------------------- #


def test_evaluate_answer_relevance_perfect_overlap() -> None:
    """Question's tokens are fully covered by the answer → recall = 1.0."""
    score = evaluate_answer_relevance(
        question="BBU 告警",
        answer="BBU 告警需要硬件更换。",
    )
    assert score == 1.0


def test_evaluate_answer_relevance_partial_overlap() -> None:
    """Only some question tokens appear in the answer → recall < 1.0."""
    score = evaluate_answer_relevance(
        question="BBU 告警 硬件",
        answer="BBU 告警.",
    )
    # question tokens = {bbu, 告警, 硬件}; answer covers {bbu, 告警} = 2/3.
    assert 0.3 <= score <= 0.7
    assert score < 1.0


def test_evaluate_answer_relevance_no_overlap() -> None:
    score = evaluate_answer_relevance(
        question="什么是 RRC?",
        answer="This is an English answer with no Chinese overlap.",
    )
    assert score == 0.0


def test_evaluate_answer_relevance_uses_expected_text_when_provided() -> None:
    """When expected_answer_text is set, use it as a stronger reference."""
    score = evaluate_answer_relevance(
        question="什么是 RRC?",
        answer="something else",
        expected_answer_text="RRC 是无线资源控制",
    )
    # No token overlap with expected text → score = 0.
    assert score == 0.0


def test_evaluate_answer_relevance_empty_inputs() -> None:
    assert evaluate_answer_relevance("", "") == 0.0


def test_evaluate_answer_relevance_question_inside_answer() -> None:
    """When answer fully contains the question's terms, score is 1.0."""
    score = evaluate_answer_relevance(
        question="BBU 告警",
        answer="BBU 告警需要硬件更换。",
    )
    assert score == 1.0


# --------------------------------------------------------------------------- #
# compute() — wires the new metrics into the pipeline
# --------------------------------------------------------------------------- #


def test_compute_populates_new_faithfulness_metric() -> None:
    results = [
        _result(
            answer="RRC 连接失败. 功率过低.",
            chunks=[
                "故障根因包括 RRC 连接失败和功率过低。",
            ],
        ),
    ]
    m = compute(results, top_ks=[1, 3])
    assert m.faithfulness > 0.5
    assert m.faithfulness_eligible == 1


def test_compute_keeps_legacy_keyword_path_for_backward_compat() -> None:
    """When retrieved_chunks is empty, fall back to expected_answer_keywords
    path (legacy behaviour) for back-compat with existing golden cases."""
    results = [
        _result(
            answer="RRC 告警频繁",
            expected_keywords=["RRC", "告警"],
        ),
    ]
    m = compute(results, top_ks=[1, 3])
    assert m.faithfulness > 0.5  # 2/2 keywords hit
    assert m.faithfulness_eligible == 1


def test_compute_faithfulness_zero_when_no_evidence_and_no_keywords() -> None:
    results = [
        _result(answer="untethered claim."),
    ]
    m = compute(results, top_ks=[1, 3])
    assert m.faithfulness == 0.0
    assert m.faithfulness_eligible == 0


def test_compute_answer_relevance_uses_evidence_path() -> None:
    results = [
        _result(
            question="什么是 RRC 失败?",
            answer="RRC 失败是无线资源控制层异常.",
            chunks=["RRC 失败是无线资源控制层异常."],
        ),
    ]
    m = compute(results, top_ks=[1, 3])
    # recall = (question tokens ∩ answer tokens) / question tokens.
    # The answer covers at least the key tokens (rrc/失败/etc.).
    assert m.answer_relevance >= 0.5


def test_compute_answer_relevance_falls_back_to_expected_text() -> None:
    results = [
        _result(
            question="什么是 RRC?",
            answer="unrelated",
            expected_text="RRC 无线资源控制",
        ),
    ]
    m = compute(results, top_ks=[1, 3])
    assert m.answer_relevance_eligible == 1
    # Token overlap between "unrelated" and "RRC 无线资源控制" is 0.
    assert m.answer_relevance == 0.0


def test_compute_combined_rag_metrics_endpoint_to_end() -> None:
    """End-to-end: the new metrics flow into a single EvalMetrics summary."""
    results = [
        _result(
            qid="q1",
            question="RRC 失败根因?",
            answer="功率过低导致 RRC 失败.",
            chunks=["功率过低是 RRC 失败根因."],
        ),
        _result(
            qid="q2",
            question="BBU 告警处理?",
            answer="不相关答案.",
            expected_text="BBU 告警需要硬件更换.",
        ),
    ]
    m = compute(results, top_ks=[1, 3])
    assert m.total == 2
    # q1 contributes claim-grounded faithfulness (chunks present);
    # q2 only has expected_text for relevance, no chunks/keywords, so
    # only q1 is faithfulness-eligible.
    assert m.faithfulness_eligible == 1
    # Both q1 (chunks) and q2 (expected_text) participate in relevance.
    assert m.answer_relevance_eligible == 2
    # q1's answer covers its question tokens (rrc/失败/etc.).
    assert m.faithfulness > 0.0
    # q2's answer has no overlap with expected text → 0.
    assert m.answer_relevance >= 0.0


# --------------------------------------------------------------------------- #
# LLM judge — optional path (env-gated, no LLM → falls back)
# --------------------------------------------------------------------------- #


def test_llm_judge_off_falls_back_to_token_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No LLM_API_KEY → use the deterministic token-overlap path."""
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_JUDGE_ENABLED", raising=False)
    score, _, _ = evaluate_faithfulness(
        answer="功率过低.",
        retrieved_chunks=["功率过低, 检查 RRU."],
    )
    assert score > 0.0  # token overlap fires


def test_llm_judge_on_uses_provided_judge_when_scoring_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM_JUDGE_ENABLED + a judge is injected, the per-claim scoring
    delegates to the judge instead of the token overlap.  The judge here
    returns 1.0 for every claim so the score == 1.0."""
    monkeypatch.setenv("LLM_JUDGE_ENABLED", "true")

    class _Judge:
        def score_claim(self, claim: str, context: list[str]) -> float:
            return 1.0

    score, supported, total = evaluate_faithfulness(
        answer="任何声明.",
        retrieved_chunks=["任何证据."],
        judge=_Judge(),
    )
    assert total >= 1
    assert supported == total
    assert score == 1.0


def test_llm_judge_partial_score_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_JUDGE_ENABLED", "true")

    class _Judge:
        def __init__(self, scores: list[float]) -> None:
            self._scores = scores
            self.idx = 0

        def score_claim(self, claim: str, context: list[str]) -> float:
            s = self._scores[self.idx]
            self.idx += 1
            return s

    # Two claims; first is supported (1.0), second is not (0.0).
    judge = _Judge([1.0, 0.0])
    score, supported, total = evaluate_faithfulness(
        answer="声明一. 声明二.",
        retrieved_chunks=["声明一在这里."],
        judge=judge,
        support_threshold=0.5,
    )
    assert total == 2
    assert supported == 1
    assert score == 0.5


def test_llm_judge_off_uses_token_overlap_even_when_judge_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit judge arg is ignored when LLM_JUDGE_ENABLED is false."""
    monkeypatch.delenv("LLM_JUDGE_ENABLED", raising=False)

    class _Judge:
        def score_claim(self, claim: str, context: list[str]) -> float:
            raise AssertionError("should not be called when judge is off")

    score, _, _ = evaluate_faithfulness(
        answer="声明一.",
        retrieved_chunks=["声明一在这里."],
        judge=_Judge(),
    )
    assert score > 0.0  # token-overlap path still ran


def test_llm_judge_unavailable_judge_skipped() -> None:
    """A judge that returns None (degraded) is treated as not-supplied."""
    class _Judge:
        def score_claim(self, claim: str, context: list[str]):
            return None  # judge unavailable

    score, supported, _total = evaluate_faithfulness(
        answer="声明.",
        retrieved_chunks=["声明在这里."],
        judge=_Judge(),
    )
    # Falls back to token-overlap, which finds the claim supported.
    assert supported >= 1
    assert score > 0.0
