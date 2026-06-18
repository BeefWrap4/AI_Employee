"""Eval metrics for RAG + RCA goldens (spec §5.6 + R18-4).

This module owns the offline scoring math.  The pipeline runs the
``compute`` function over a list of :class:`EvalResult` and returns an
:class:`EvalMetrics` summary, plus two public helpers used by
:mod:`ai_employee.eval.runner` and downstream reporting:

* :func:`evaluate_faithfulness` — RAGAS-style claim-grounding: split the
  answer into atomic claims, score each as supported / unsupported by
  the retrieved chunks.  Returns ``(score, supported, total)``.  When an
  LLM judge is provided AND :func:`_llm_judge_enabled`, the per-claim
  score comes from the judge; otherwise a deterministic token-overlap
  path runs.
* :func:`evaluate_answer_relevance` — Jaccard token overlap between the
  answer and the question (or the expected answer text when supplied).
* :func:`split_claims` — lightweight deterministic sentence/segment
  splitter (RAGAS-lite).

Faithfulness / answer-relevance started as toy keyword-overlap
implementations.  R18-4 upgrades them to claim-grounding + question
overlap; the toy path is preserved as the fallback when no retrieved
chunks or expected text are available so legacy golden cases still
score.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

# --------------------------------------------------------------------------- #
# Optional LLM-judge interface (R18-4 / spec §5.6)
# --------------------------------------------------------------------------- #


class ClaimJudge(Protocol):
    """Verifies whether a claim is supported by the retrieved context.

    Implementations return a score in ``[0.0, 1.0]`` (1.0 = fully
    supported, 0.0 = contradicts evidence) or ``None`` when the judge
    is itself unavailable (e.g. LLM call failed).  A ``None`` return
    is treated as "no signal" and the pipeline falls back to the
    deterministic token-overlap path.
    """

    def score_claim(self, claim: str, context: list[str]) -> float | None: ...


def _llm_judge_enabled() -> bool:
    """True when an LLM judge should be used (env-gated)."""
    return os.getenv("LLM_JUDGE_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _llm_judge_available() -> bool:
    """An LLM judge is available iff enabled + a key is configured."""
    if not _llm_judge_enabled():
        return False
    return bool(
        os.getenv("QWEN_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("SILICONFLOW_API_KEY")
    )


# --------------------------------------------------------------------------- #
# Claim splitting — atomic-fact extractor
# --------------------------------------------------------------------------- #

# Sentence/segment terminators: Chinese (。！？；), English (. ! ? ;),
# newlines.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？.!?;；])\s*|(?:\n+)")


def split_claims(text: str) -> list[str]:
    """Split an answer into atomic claim strings.

    RAGAS-style claim extraction: we keep the lightweight rule-based
    approach (sentence/segment boundaries) because it is deterministic
    and needs no model.  Returns the original lowercased segments
    (whitespace-trimmed, non-empty).  An LLM-based splitter can be
    plugged in later by replacing this function.
    """
    if not text:
        return []
    text = text.strip().lower()
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


# --------------------------------------------------------------------------- #
# Token overlap helpers (RAGAS-lite)
# --------------------------------------------------------------------------- #


def _tokenize(text: str) -> set[str]:
    """Whitespace-split for ASCII; char-level for CJK (so 中文 chunks overlap
    meaningfully on shared characters).

    Emits both single-CJK and bigram-CJK tokens to balance precision
    (bigrams) and recall (unigrams) on a short question/answer overlap.
    """
    out: set[str] = set()
    if not text:
        return out
    text = text.lower()
    # English / digits: word tokens.
    for m in re.finditer(r"[a-z0-9_]+", text):
        if len(m.group(0)) > 1:
            out.add(m.group(0))
    # CJK: single chars + 2-grams (spec §5.4 tokenization hint).
    chars: list[str] = []
    for ch in text:
        if "一" <= ch <= "鿿":
            chars.append(ch)
    for c in chars:
        out.add(c)
    for i in range(len(chars) - 1):
        out.add(chars[i] + chars[i + 1])
    return out


def _claim_supported(claim: str, chunks: list[str]) -> bool:
    """A claim is 'supported' if it has substantial token overlap with
    any retrieved chunk.  Uses the *best* overlap across chunks.
    """
    if not chunks:
        return False
    claim_tokens = _tokenize(claim)
    if not claim_tokens:
        return False
    best = 0.0
    for chunk in chunks:
        chunk_tokens = _tokenize(chunk)
        if not chunk_tokens:
            continue
        union = claim_tokens | chunk_tokens
        if not union:
            continue
        jaccard = len(claim_tokens & chunk_tokens) / len(union)
        if jaccard > best:
            best = jaccard
    # Threshold: any non-trivial token overlap counts as supported.
    # The RAGAS-lite path is intentionally lenient (better to over-credit
    # than under-credit when ground-truth is just text).
    return best >= 0.05


# --------------------------------------------------------------------------- #
# Faithfulness — claim grounding (RAGAS-style)
# --------------------------------------------------------------------------- #


def evaluate_faithfulness(
    answer: str,
    retrieved_chunks: list[str],
    *,
    judge: ClaimJudge | None = None,
    support_threshold: float = 0.5,
) -> tuple[float, int, int]:
    """Score how much of the answer is grounded by the retrieved chunks.

    Returns ``(score, supported_count, total_claims)``.

    * When ``judge`` is supplied AND :func:`_llm_judge_enabled`, each
      claim's support score comes from the judge.
    * Otherwise, the deterministic token-overlap path runs
      (:func:`_claim_supported`).

    A claim whose support score is ``>= support_threshold`` counts as
    supported.
    """
    claims = split_claims(answer)
    if not claims:
        return 0.0, 0, 0
    use_judge = judge is not None and _llm_judge_enabled()
    supported = 0
    for claim in claims:
        if use_judge:
            try:
                s = judge.score_claim(claim, retrieved_chunks)
            except Exception:
                s = None
            if s is None:
                # Judge unavailable for this claim → fall back to tokens.
                if _claim_supported(claim, retrieved_chunks):
                    supported += 1
                continue
            if s >= support_threshold:
                supported += 1
        else:
            if _claim_supported(claim, retrieved_chunks):
                supported += 1
    return supported / len(claims), supported, len(claims)


# --------------------------------------------------------------------------- #
# Answer relevance — question/answer overlap
# --------------------------------------------------------------------------- #


def evaluate_answer_relevance(
    question: str,
    answer: str,
    *,
    expected_answer_text: str | None = None,
) -> float:
    """Score how well the answer addresses the question.

    Uses **token recall** (RAGAS-style): of the question's tokens, how
    many appear in the answer.  When ``expected_answer_text`` is
    supplied, the reference is the expected answer (the question's
    topicality is judged against what a correct answer would cover).

    Returns a value in ``[0.0, 1.0]``.
    """
    if not question or not answer:
        return 0.0
    ans_tokens = _tokenize(answer)
    reference = expected_answer_text or question
    ref_tokens = _tokenize(reference)
    if not ans_tokens or not ref_tokens:
        return 0.0
    return len(ans_tokens & ref_tokens) / len(ref_tokens)


# --------------------------------------------------------------------------- #
# EvalResult + EvalMetrics
# --------------------------------------------------------------------------- #


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
    # Optional ground-truth answers for faithfulness / answer relevance.
    # When set, the :func:`compute` pipeline scores the answer against
    # these strings.
    expected_answer_keywords: list[str] = field(default_factory=list)
    expected_answer_text: str | None = None
    # R18-4: retrieved chunks ground the answer (claim-grading source).
    retrieved_chunks: list[str] = field(default_factory=list)


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
    # Faithfulness / answer-relevance (R18-4 / spec §5.6): upgraded to
    # RAGAS-style claim-grounding when retrieved_chunks are supplied,
    # falling back to the legacy keyword/token path for back-compat.
    faithfulness: float = 0.0
    answer_relevance: float = 0.0
    faithfulness_eligible: int = 0
    answer_relevance_eligible: int = 0
    # Audit: which path computed each metric.
    faithfulness_method: str = "none"  # one of {"claims", "keywords", "none"}
    answer_relevance_method: str = "none"  # one of {"reference", "question", "none"}
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
    lo = math.floor(rank)
    hi = math.ceil(rank)
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


def compute(
    results: list[EvalResult],
    top_ks: list[int],
    *,
    claim_judge: ClaimJudge | None = None,
) -> EvalMetrics:
    m = EvalMetrics(total=len(results))
    for k in top_ks:
        m.hit_counts[k] = 0
        m.hit_rates[k] = 0.0

    hit_eligible: list[EvalResult] = []
    refusal_expected: list[EvalResult] = []
    covered = 0
    latencies: list[int] = []

    # Track which method was used (per the highest-eligible result).
    faithfulness_method = "none"
    answer_relevance_method = "none"

    for r in results:
        latencies.append(r.latency_ms)
        if r.error or r.status_code == 0:
            m.errored += 1
            m.per_item.append(
                {
                    "qid": r.qid,
                    "verdict": "error",
                    "expected": r.expected_doc_id,
                    "returned": [],
                    "latency_ms": r.latency_ms,
                    "error": r.error,
                }
            )
            continue

        if r.expect_refusal:
            refusal_expected.append(r)
            if r.status_code == 404:
                m.refusal_correct += 1
                m.per_item.append(
                    {
                        "qid": r.qid,
                        "verdict": "refusal",
                        "expected": None,
                        "returned": [],
                        "status_code": r.status_code,
                        "latency_ms": r.latency_ms,
                    }
                )
            else:
                m.refusal_violations += 1
                m.per_item.append(
                    {
                        "qid": r.qid,
                        "verdict": "refusal_violation",
                        "expected": None,
                        "returned": r.returned_doc_ids,
                        "status_code": r.status_code,
                        "latency_ms": r.latency_ms,
                    }
                )
            continue

        hit_eligible.append(r)
        verdict = _hit_verdict(r.returned_doc_ids, r.expected_doc_id or "", top_ks)
        for k in top_ks:
            if r.expected_doc_id and r.expected_doc_id in r.returned_doc_ids[:k]:
                m.hit_counts[k] += 1
        if r.expected_doc_id and r.returned_doc_ids and r.expected_doc_id in r.returned_doc_ids:
            covered += 1

        # Faithfulness (R18-4 / spec §5.6): RAGAS-style claim-grounding
        # when retrieved_chunks are present; legacy keyword path otherwise.
        if r.retrieved_chunks:
            score, supported, total = evaluate_faithfulness(
                r.answer, r.retrieved_chunks, judge=claim_judge,
            )
            if total > 0:
                m.faithfulness += score
                m.faithfulness_eligible += 1
                if faithfulness_method == "none":
                    faithfulness_method = "claims"
        elif r.expected_answer_keywords:
            answer_lower = r.answer.lower()
            hit_kw = sum(
                1 for kw in r.expected_answer_keywords
                if kw.lower() in answer_lower
            )
            m.faithfulness += hit_kw / len(r.expected_answer_keywords)
            m.faithfulness_eligible += 1
            if faithfulness_method == "none":
                faithfulness_method = "keywords"

        # Answer relevance (R18-4): reference text when supplied, else question.
        if r.expected_answer_text or r.question:
            rel = evaluate_answer_relevance(
                r.question, r.answer,
                expected_answer_text=r.expected_answer_text,
            )
            m.answer_relevance += rel
            m.answer_relevance_eligible += 1
            if r.expected_answer_text and answer_relevance_method == "none":
                answer_relevance_method = "reference"
            elif r.question and answer_relevance_method == "none":
                answer_relevance_method = "question"

        m.per_item.append(
            {
                "qid": r.qid,
                "verdict": verdict,
                "expected": r.expected_doc_id,
                "returned": r.returned_doc_ids,
                "latency_ms": r.latency_ms,
            }
        )

    m.eligible_for_hit = len(hit_eligible)
    for k in top_ks:
        m.hit_rates[k] = m.hit_counts[k] / m.eligible_for_hit if m.eligible_for_hit else 0.0
    m.refusal_expected = len(refusal_expected)
    m.refusal_accuracy = m.refusal_correct / m.refusal_expected if m.refusal_expected else 0.0
    m.citation_coverage = covered / m.eligible_for_hit if m.eligible_for_hit else 0.0
    m.faithfulness = (
        m.faithfulness / m.faithfulness_eligible
        if m.faithfulness_eligible else 0.0
    )
    m.answer_relevance = (
        m.answer_relevance / m.answer_relevance_eligible
        if m.answer_relevance_eligible else 0.0
    )
    m.faithfulness_method = faithfulness_method
    m.answer_relevance_method = answer_relevance_method

    if latencies:
        m.latency_p50_ms = _percentile([float(x) for x in latencies], 50)
        m.latency_p95_ms = _percentile([float(x) for x in latencies], 95)
        m.latency_mean_ms = sum(latencies) / len(latencies)
    return m


__all__ = [
    "ClaimJudge",
    "EvalMetrics",
    "EvalResult",
    "_percentile",
    "compute",
    "evaluate_answer_relevance",
    "evaluate_faithfulness",
    "split_claims",
]
