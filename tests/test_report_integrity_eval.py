"""Report-integrity eval tests (R18-2 / spec §5.7).

RCA report structure & citation completeness:

Required fields per spec §6.6:
- ``title`` (str, non-empty)
- ``summary`` (str, non-empty) — incident summary
- ``root_causes`` (≥1, each with description + supporting_evidence_ids)
- ``evidence_chain`` (≥1, each with content + source/chunk_id)
- ``top_n`` (int ≥ 1)
- ``review_status`` (str, non-empty)

Spec §6.6 also requires:
- ``affected_scope`` — the impact range, non-empty
- ``timeline`` — key events timeline, non-empty
- ``source`` — citation source, non-empty (or chunk_id on each evidence)

The runner is decoupled from the platform's RcaStore; the eval accepts
a :class:`ReportIntegrityInputs` so it stays unit-testable.
"""

from __future__ import annotations

import pytest
from ai_employee.eval.runner import (
    ReportIntegrityInputs,
    evaluate_report_integrity,
)


def _inputs(
    *,
    title: str = "Incident report",
    summary: str = "Brief summary of the incident.",
    root_causes: list[dict] | None = "default",
    evidence_chain: list[dict] | None = "default",
    top_n: int = 3,
    review_status: str = "accepted",
    affected_scope: str = "SITE-001",
    timeline: list[dict] | None = "default",
    source: str = "RCA agent",
) -> ReportIntegrityInputs:
    # ``"default"`` sentinel means "use the canonical happy-path value";
    # pass an explicit list (including []) to test other cases.
    return ReportIntegrityInputs(
        title=title,
        summary=summary,
        root_causes=[
            {
                "description": "PRB capacity exhausted",
                "supporting_evidence_ids": ["ev-001"],
            },
        ]
        if root_causes == "default"
        else root_causes,
        evidence_chain=[
            {"chunk_id": "ev-001", "content": "KPI spike 12:00-13:00", "source": "kpi"},
        ]
        if evidence_chain == "default"
        else evidence_chain,
        top_n=top_n,
        review_status=review_status,
        affected_scope=affected_scope,
        timeline=[{"ts": "2026-06-17T10:00", "event": "alarm"}]
        if timeline == "default"
        else (timeline or []),
        source=source,
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_complete_report_passes() -> None:
    v = evaluate_report_integrity(_inputs())
    assert v.passed is True
    assert v.missing_required == []
    assert v.completeness == 1.0


# --------------------------------------------------------------------------- #
# Required fields
# --------------------------------------------------------------------------- #


def test_missing_title_fails() -> None:
    v = evaluate_report_integrity(_inputs(title=""))
    assert v.passed is False
    assert "title" in v.missing_required


def test_missing_summary_fails() -> None:
    v = evaluate_report_integrity(_inputs(summary=""))
    assert v.passed is False
    assert "summary" in v.missing_required


def test_missing_root_causes_fails() -> None:
    v = evaluate_report_integrity(_inputs(root_causes=[]))
    assert v.passed is False
    assert "root_causes" in v.missing_required


def test_root_cause_with_no_supporting_evidence_fails() -> None:
    v = evaluate_report_integrity(
        _inputs(
            root_causes=[{"description": "PRB cap", "supporting_evidence_ids": []}],
        ),
    )
    assert v.passed is False
    assert any("evidence" in m for m in v.structural_issues)


def test_missing_evidence_chain_fails() -> None:
    v = evaluate_report_integrity(_inputs(evidence_chain=[]))
    assert v.passed is False
    assert "evidence_chain" in v.missing_required


def test_evidence_with_no_citation_fails() -> None:
    v = evaluate_report_integrity(
        _inputs(evidence_chain=[{"content": "kpi spike", "source": ""}]),
    )
    assert v.passed is False
    assert any("citation" in m for m in v.structural_issues)


def test_top_n_zero_fails() -> None:
    v = evaluate_report_integrity(_inputs(top_n=0))
    assert v.passed is False
    assert any("top_n" in m for m in v.missing_required)


def test_missing_review_status_fails() -> None:
    v = evaluate_report_integrity(_inputs(review_status=""))
    assert v.passed is False
    assert "review_status" in v.missing_required


# --------------------------------------------------------------------------- #
# Spec §6.6 — affected_scope, timeline, source
# --------------------------------------------------------------------------- #


def test_missing_affected_scope_fails() -> None:
    v = evaluate_report_integrity(_inputs(affected_scope=""))
    assert v.passed is False
    assert "affected_scope" in v.missing_required


def test_missing_timeline_fails() -> None:
    v = evaluate_report_integrity(_inputs(timeline=[]))
    assert v.passed is False
    assert "timeline" in v.missing_required


def test_missing_source_fails() -> None:
    v = evaluate_report_integrity(_inputs(source=""))
    assert v.passed is False
    assert "source" in v.missing_required


# --------------------------------------------------------------------------- #
# Completeness scoring
# --------------------------------------------------------------------------- #


def test_completeness_drops_with_missing_fields() -> None:
    """Each missing required field drops the completeness score."""
    v = evaluate_report_integrity(
        _inputs(title="", summary="", root_causes=[]),
    )
    # 3 of 9 required fields missing → completeness = 6/9.
    assert v.passed is False
    assert v.completeness == pytest.approx(6 / 9)


def test_completeness_zero_when_all_missing() -> None:
    v = evaluate_report_integrity(
        _inputs(
            title="",
            summary="",
            root_causes=[],
            evidence_chain=[],
            top_n=0,
            review_status="",
            affected_scope="",
            timeline=[],
            source="",
        ),
    )
    assert v.completeness == 0.0


# --------------------------------------------------------------------------- #
# Runner integration
# --------------------------------------------------------------------------- #


def test_run_eval_dispatches_report() -> None:
    from ai_employee.eval.runner import EvalRunRequest, run_eval

    req = EvalRunRequest(
        eval_type="report",
        template_id="rca-001",
        report_inputs=_inputs(),
    )
    summary = run_eval(req)
    assert summary.eval_type == "report"
    assert summary.report_integrity is not None
    assert summary.report_integrity.passed is True
