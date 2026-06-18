"""A/B testing harness tests."""
from __future__ import annotations

import pytest

from ai_employee.agent_platform_api.ab_testing import (
    ABExperiment,
    ABExperimentStore,
    VariantAssignment,
    analyze_two_proportion_z_test,
    assign_variant,
    build_ab_store,
    two_sample_z_score,
)


# --------------------------------------------------------------------------- #
# Variant assignment
# --------------------------------------------------------------------------- #


def test_assign_variant_control_split_zero() -> None:
    """100% control: every bucket lands in control."""
    exp = ABExperiment(
        experiment_id="exp_1",
        control="control",
        treatment="treatment",
        traffic_split=0.0,
    )
    for _ in range(100):
        v = assign_variant(exp, bucket_key="user")
        assert v == "control"


def test_assign_variant_treatment_split_one() -> None:
    exp = ABExperiment(
        experiment_id="exp_1",
        control="control",
        treatment="treatment",
        traffic_split=1.0,
    )
    for _ in range(100):
        assert assign_variant(exp, bucket_key="user") == "treatment"


def test_assign_variant_deterministic_per_bucket() -> None:
    """Same bucket_key always returns the same variant."""
    exp = ABExperiment(
        experiment_id="exp_1",
        control="control",
        treatment="treatment",
        traffic_split=0.5,
    )
    first = assign_variant(exp, bucket_key="alice")
    for _ in range(10):
        assert assign_variant(exp, bucket_key="alice") == first


def test_assign_variant_roughly_respects_split() -> None:
    exp = ABExperiment(
        experiment_id="exp_1",
        control="control",
        treatment="treatment",
        traffic_split=0.5,
    )
    n_treatment = sum(
        1 for i in range(2000) if assign_variant(exp, bucket_key=f"u{i}") == "treatment"
    )
    # 1000 ± 100 is well within statistical tolerance for 2000 trials.
    assert 800 <= n_treatment <= 1200


def test_assign_variant_returns_variant_name() -> None:
    exp = ABExperiment(
        experiment_id="exp_1",
        control="baseline",
        treatment="variant_a",
        traffic_split=0.5,
    )
    v = assign_variant(exp, bucket_key="alice")
    assert v in {"baseline", "variant_a"}


def test_assign_variant_rejects_invalid_split() -> None:
    with pytest.raises(ValueError):
        ABExperiment(
            experiment_id="x", control="c", treatment="t", traffic_split=1.5,
        )


# --------------------------------------------------------------------------- #
# Statistical significance
# --------------------------------------------------------------------------- #


def test_two_sample_z_score_identical_distributions_is_zero() -> None:
    """When both samples are identical, the z-score should be ~0."""
    z = two_sample_z_score(mean_a=10, var_a=4, n_a=100, mean_b=10, var_b=4, n_b=100)
    assert abs(z) < 1e-9


def test_two_sample_z_score_large_difference_is_significant() -> None:
    z = two_sample_z_score(mean_a=10, var_a=4, n_a=100, mean_b=20, var_b=4, n_b=100)
    assert abs(z) > 5.0


def test_two_sample_z_score_handles_zero_variance() -> None:
    z = two_sample_z_score(mean_a=5, var_a=0, n_a=10, mean_b=5, var_b=0, n_b=10)
    assert z == 0.0


def test_analyze_two_proportion_significant() -> None:
    result = analyze_two_proportion_z_test(
        successes_a=50, n_a=100, successes_b=80, n_b=100,
    )
    assert result.z_score is not None
    # 50/100 vs 80/100 with n=100 each → z ≈ 4.45, well above the
    # 95% threshold (1.96) but below the artificial 5.0 bound.
    assert abs(result.z_score) > 3.0
    assert result.p_value is not None
    assert result.p_value < 0.001
    assert result.is_significant(alpha=0.05) is True


def test_analyze_two_proportion_not_significant() -> None:
    result = analyze_two_proportion_z_test(
        successes_a=49, n_a=100, successes_b=51, n_b=100,
    )
    assert result.is_significant(alpha=0.05) is False


def test_analyze_two_proportion_serializable() -> None:
    result = analyze_two_proportion_z_test(
        successes_a=10, n_a=100, successes_b=20, n_b=100,
    )
    d = result.to_dict()
    assert "z_score" in d
    assert "p_value" in d
    assert "is_significant" in d


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_store_create_and_list() -> None:
    store = ABExperimentStore()
    exp = store.create(
        experiment_id="exp_1",
        control="control",
        treatment="treatment",
        traffic_split=0.5,
    )
    assert exp.experiment_id == "exp_1"
    assert store.list_all() == [exp]


def test_store_record_outcome() -> None:
    store = ABExperimentStore()
    store.create(
        experiment_id="exp_1",
        control="c", treatment="t", traffic_split=0.5,
    )
    store.record_outcome("exp_1", "control", "ctr", 0.5)
    store.record_outcome("exp_1", "control", "ctr", 0.6)
    store.record_outcome("exp_1", "treatment", "ctr", 0.7)
    outcomes = store.list_outcomes("exp_1")
    assert len(outcomes) == 3


def test_store_record_outcome_unknown_experiment_ignored() -> None:
    store = ABExperimentStore()
    store.record_outcome("ghost", "control", "ctr", 0.5)
    assert store.list_outcomes("ghost") == []


def test_store_summary_groups_by_variant() -> None:
    store = ABExperimentStore()
    store.create(
        experiment_id="exp_1", control="c", treatment="t", traffic_split=0.5,
    )
    for v in (0.1, 0.2, 0.3):
        store.record_outcome("exp_1", "c", "ctr", v)
    for v in (0.4, 0.5, 0.6):
        store.record_outcome("exp_1", "t", "ctr", v)
    summary = store.summarise("exp_1", metric_name="ctr")
    assert summary is not None
    assert "c" in summary
    assert "t" in summary
    assert summary["c"]["count"] == 3
    assert summary["t"]["count"] == 3


def test_build_ab_store_returns_singleton() -> None:
    a = build_ab_store()
    b = build_ab_store()
    assert a is b


def test_variant_assignment_dataclass() -> None:
    va = VariantAssignment(experiment_id="exp_1", variant="control")
    assert va.experiment_id == "exp_1"
    assert va.variant == "control"