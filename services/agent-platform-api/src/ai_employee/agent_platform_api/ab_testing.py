"""A/B testing harness (spec §5.9).

Two pieces:

* :class:`ABExperiment` + :func:`assign_variant` — deterministic
  bucketing of subjects into ``control`` / ``treatment``.  The hash of
  ``(experiment_id, bucket_key)`` is compared against the traffic split,
  so the same subject always sees the same variant until the experiment
  is changed.
* :func:`two_sample_z_score` and
  :func:`analyze_two_proportion_z_test` — pure-Python statistical
  helpers so we don't pull in scipy/numpy for a single z-test.

:class:`ABExperimentStore` is the in-process registry that callers use
to create experiments, record outcomes, and pull per-variant summaries.
"""
from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# --------------------------------------------------------------------------- #
# Variant assignment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ABExperiment:
    experiment_id: str
    control: str
    treatment: str
    traffic_split: float  # 0.0 = all control, 1.0 = all treatment

    def __post_init__(self) -> None:
        if not 0.0 <= self.traffic_split <= 1.0:
            raise ValueError(
                f"traffic_split must be in [0, 1], got {self.traffic_split}",
            )


@dataclass(frozen=True)
class VariantAssignment:
    experiment_id: str
    variant: str


def _bucket_score(experiment_id: str, bucket_key: str) -> float:
    """Map ``(experiment_id, bucket_key)`` to a value in ``[0, 1)``.

    Stable across processes / restarts because the hash input is the
    same string every time.
    """
    payload = f"{experiment_id}:{bucket_key}".encode()
    digest = hashlib.sha256(payload).digest()
    # Use the first 8 bytes as a 64-bit integer; divide by 2^64.
    n = int.from_bytes(digest[:8], "big")
    return n / float(1 << 64)


def assign_variant(exp: ABExperiment, *, bucket_key: str) -> str:
    """Return ``exp.treatment`` for ``traffic_split`` fraction of subjects,
    ``exp.control`` otherwise.  Deterministic per bucket.
    """
    score = _bucket_score(exp.experiment_id, bucket_key)
    return exp.treatment if score < exp.traffic_split else exp.control


# --------------------------------------------------------------------------- #
# Statistical helpers
# --------------------------------------------------------------------------- #


def two_sample_z_score(
    *,
    mean_a: float,
    var_a: float,
    n_a: int,
    mean_b: float,
    var_b: float,
    n_b: int,
) -> float:
    """Two-sample z-test for difference of means (pooled variance)."""
    if n_a <= 0 or n_b <= 0:
        return 0.0
    if var_a == 0 and var_b == 0:
        return 0.0
    pooled_var = ((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)
    if pooled_var <= 0:
        return 0.0
    standard_error = math.sqrt(pooled_var * (1.0 / n_a + 1.0 / n_b))
    if standard_error == 0:
        return 0.0
    return (mean_a - mean_b) / standard_error


@dataclass
class ProportionTestResult:
    z_score: float | None
    p_value: float | None

    def is_significant(self, alpha: float = 0.05) -> bool:
        """Return True when the two-tailed p-value is below ``alpha``."""
        if self.p_value is None:
            return False
        return self.p_value < alpha

    def to_dict(self) -> dict[str, Any]:
        return {
            "z_score": self.z_score,
            "p_value": self.p_value,
            "is_significant": self.is_significant(),
        }


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _two_tailed_p(z: float) -> float:
    return 2.0 * (1.0 - _normal_cdf(abs(z)))


def analyze_two_proportion_z_test(
    *,
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
) -> ProportionTestResult:
    """Two-proportion z-test for conversion-rate style metrics."""
    if n_a <= 0 or n_b <= 0:
        return ProportionTestResult(z_score=None, p_value=None)
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    pooled = (successes_a + successes_b) / (n_a + n_b)
    if pooled in (0.0, 1.0):
        return ProportionTestResult(z_score=0.0, p_value=1.0)
    standard_error = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b))
    if standard_error == 0:
        return ProportionTestResult(z_score=0.0, p_value=1.0)
    z = (p_a - p_b) / standard_error
    p_value = _two_tailed_p(z)
    return ProportionTestResult(z_score=z, p_value=p_value)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


@dataclass
class OutcomeRecord:
    experiment_id: str
    variant: str
    metric_name: str
    value: float
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ABExperimentStore:
    """Thread-safe in-process registry of experiments + outcomes."""

    def __init__(self) -> None:
        self._experiments: dict[str, ABExperiment] = {}
        self._outcomes: list[OutcomeRecord] = []
        self._lock = threading.Lock()

    def create(
        self,
        *,
        experiment_id: str,
        control: str,
        treatment: str,
        traffic_split: float,
    ) -> ABExperiment:
        exp = ABExperiment(
            experiment_id=experiment_id,
            control=control,
            treatment=treatment,
            traffic_split=traffic_split,
        )
        with self._lock:
            self._experiments[experiment_id] = exp
        return exp

    def get(self, experiment_id: str) -> ABExperiment | None:
        with self._lock:
            return self._experiments.get(experiment_id)

    def list_all(self) -> list[ABExperiment]:
        with self._lock:
            return list(self._experiments.values())

    def record_outcome(
        self,
        experiment_id: str,
        variant: str,
        metric_name: str,
        value: float,
    ) -> None:
        with self._lock:
            if experiment_id not in self._experiments:
                return
            self._outcomes.append(
                OutcomeRecord(
                    experiment_id=experiment_id,
                    variant=variant,
                    metric_name=metric_name,
                    value=float(value),
                )
            )

    def list_outcomes(self, experiment_id: str) -> list[OutcomeRecord]:
        with self._lock:
            return [o for o in self._outcomes if o.experiment_id == experiment_id]

    def summarise(
        self, experiment_id: str, *, metric_name: str,
    ) -> dict[str, dict[str, float]] | None:
        if self.get(experiment_id) is None:
            return None
        grouped: dict[str, list[float]] = {}
        for outcome in self.list_outcomes(experiment_id):
            if outcome.metric_name != metric_name:
                continue
            grouped.setdefault(outcome.variant, []).append(outcome.value)
        out: dict[str, dict[str, float]] = {}
        for variant, values in grouped.items():
            n = len(values)
            mean = sum(values) / n if n else 0.0
            variance = (
                sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
            )
            out[variant] = {
                "count": float(n),
                "mean": mean,
                "variance": variance,
                "min": min(values) if values else 0.0,
                "max": max(values) if values else 0.0,
            }
        return out


# --------------------------------------------------------------------------- #
# Singleton + factory
# --------------------------------------------------------------------------- #

_store = ABExperimentStore()


def build_ab_store() -> ABExperimentStore:
    return _store


__all__ = [
    "ABExperiment",
    "ABExperimentStore",
    "OutcomeRecord",
    "ProportionTestResult",
    "VariantAssignment",
    "analyze_two_proportion_z_test",
    "assign_variant",
    "build_ab_store",
    "two_sample_z_score",
]
