"""Tool-call correctness eval tests (R18-1 / spec §5.7).

Drives the missing piece of the eval center: given a golden set of
expected tool names (and optionally their call order), verify that a
run actually invoked the right tools, in the right order, without
spurious extras.  The runner is decoupled from the platform's
``tool_call_log`` table — the eval accepts an abstract list of
``ToolCallSummary``-shaped records so it stays unit-testable.

Metrics
-------
* ``recall`` — of the golden tools, how many were actually called.
* ``precision`` — of the tools actually called, how many are golden.
* ``order_score`` — LCS(golden, actual) / max(|golden|, |actual|);
  only computed when ``order_required=True``; otherwise defaults to 1.0
  (order not asserted).
* ``extras`` — names the run called that the golden did not list
  (precision violations); surfaced in the per-item detail so dashboards
  can show them.
* ``missing`` — golden tools the run did not call (recall violations).
"""
from __future__ import annotations

import pytest
from ai_employee.eval.runner import (
    ToolCallCorrectness,
    evaluate_tool_call_correctness,
)


def _call(name: str) -> ToolCallCorrectness:
    return ToolCallCorrectness(tool_name=name, status="ok")


# --------------------------------------------------------------------------- #
# Recall
# --------------------------------------------------------------------------- #


def test_recall_is_zero_when_no_golden_calls() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("cmdb.lookup")],
        golden=["net.switch"],
    )
    assert result.recall == 0.0
    assert result.missing == ["net.switch"]


def test_recall_full_when_all_golden_called() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("a"), _call("b"), _call("c")],
        golden=["a", "b", "c"],
    )
    assert result.recall == 1.0
    assert result.missing == []


def test_recall_partial() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("a")],
        golden=["a", "b", "c"],
    )
    assert result.recall == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- #
# Precision
# --------------------------------------------------------------------------- #


def test_precision_full_when_only_golden_called() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("a"), _call("b")],
        golden=["a", "b"],
    )
    assert result.precision == 1.0
    assert result.extras == []


def test_precision_drops_with_extras() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("a"), _call("b"), _call("x")],
        golden=["a", "b"],
    )
    assert result.precision == pytest.approx(2 / 3)
    assert result.extras == ["x"]


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #


def test_order_full_match() -> None:
    """When order is required, exact sequence → order_score = 1.0."""
    result = evaluate_tool_call_correctness(
        actual=[_call("a"), _call("b"), _call("c")],
        golden=["a", "b", "c"],
        order_required=True,
    )
    assert result.order_score == 1.0


def test_order_lcs_partial() -> None:
    """Wrong order → LCS-based partial credit."""
    result = evaluate_tool_call_correctness(
        actual=[_call("b"), _call("a"), _call("c")],
        golden=["a", "b", "c"],
        order_required=True,
    )
    # LCS = 3 (all three in order, b-a-c vs a-b-c: LCS=2? actually a,c
    # are common in order → 2).  max = 3.  2/3.
    assert result.order_score == pytest.approx(2 / 3)


def test_order_not_required_defaults_to_one() -> None:
    """Without order_required, order_score is 1.0 (not penalised)."""
    result = evaluate_tool_call_correctness(
        actual=[_call("b"), _call("a")],
        golden=["a", "b"],
        order_required=False,
    )
    assert result.order_score == 1.0


# --------------------------------------------------------------------------- #
# Combined
# --------------------------------------------------------------------------- #


def test_overall_score_blends_recall_precision_and_order() -> None:
    """Overall = mean(recall, precision[, order])."""
    result = evaluate_tool_call_correctness(
        actual=[_call("a"), _call("b")],
        golden=["a", "b"],
        order_required=True,
    )
    # recall=1, precision=1, order=1 → overall=1.
    assert result.overall == 1.0


def test_overall_drops_with_recall_or_precision_violation() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("a")],
        golden=["a", "b"],
        order_required=True,
    )
    # recall=0.5, precision=1, order_lcs(a vs a,b)=1, max=2 → order=0.5.
    assert result.recall == 0.5
    assert result.precision == 1.0
    assert result.order_score == 0.5
    assert result.overall == pytest.approx((0.5 + 1.0 + 0.5) / 3)


def test_empty_actual_and_empty_golden_is_perfect() -> None:
    result = evaluate_tool_call_correctness(
        actual=[],
        golden=[],
    )
    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.missing == []
    assert result.extras == []


def test_empty_actual_with_golden_is_zero_recall() -> None:
    result = evaluate_tool_call_correctness(
        actual=[],
        golden=["a", "b"],
    )
    assert result.recall == 0.0
    assert result.precision == 1.0  # no actual calls = no false positives
    assert result.missing == ["a", "b"]


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #


def test_duplicate_golden_counts_once() -> None:
    """A golden tool listed twice in the expected set is satisfied by
    one actual invocation.  Recall = satisfied / deduped(golden)."""
    result = evaluate_tool_call_correctness(
        actual=[_call("a")],
        golden=["a", "a"],
    )
    assert result.recall == 1.0  # 1 satisfied / 1 unique golden


def test_duplicate_actual_counts_once_in_precision() -> None:
    result = evaluate_tool_call_correctness(
        actual=[_call("a"), _call("a")],
        golden=["a"],
    )
    # precision = (1 unique match) / (1 unique actual).
    assert result.precision == 1.0
    assert result.extras == []


# --------------------------------------------------------------------------- #
# Runner-level integration with the eval service
# --------------------------------------------------------------------------- #


def test_eval_runner_supports_tool_call_type() -> None:
    from ai_employee.eval.runner import EvalRunRequest, run_eval

    req = EvalRunRequest(
        eval_type="tool_call",
        template_id="rca-001",
        golden_tool_calls=["cmdb.lookup", "kpi.query"],
        actual_tool_calls=["cmdb.lookup", "ticket.history.search"],
        order_required=False,
    )
    result = run_eval(req)
    assert result.eval_type == "tool_call"
    assert result.tool_call_correctness is not None
    assert result.tool_call_correctness.recall == 0.5
    assert result.tool_call_correctness.extras == ["ticket.history.search"]
    assert result.tool_call_correctness.missing == ["kpi.query"]
