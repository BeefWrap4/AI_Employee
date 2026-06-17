"""Lightweight Prometheus-style metrics registry.

Implements ``Counter``, ``Gauge`` and ``Histogram`` primitives without
pulling the heavy ``prometheus_client`` dependency.  Metrics can be
serialised to the Prometheus text exposition format via
:func:`render_prometheus_text`.

Use ``configure_default_registry()`` at process start to install a
process-wide default registry, then access primitives via the helper
functions (:func:`counter`, :func:`gauge`, :func:`histogram`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable


@dataclass
class _Sample:
    value: float
    labels: dict[str, str]


@dataclass
class _Series:
    name: str
    kind: str  # "counter" | "gauge" | "histogram"
    help_text: str
    samples: list[_Sample] = field(default_factory=list)
    # Histograms carry an explicit bucket layout.
    buckets: list[float] | None = None
    bucket_counts: dict[float, int] = field(default_factory=dict)
    sum_value: float = 0.0
    count: int = 0

    def add_sample(self, value: float, labels: dict[str, str]) -> None:
        self.samples.append(_Sample(value=value, labels=labels))


class MetricRegistry:
    """A simple, thread-safe metrics registry."""

    def __init__(self) -> None:
        self._series: dict[str, _Series] = {}
        self._lock = Lock()

    # -- registration ----------------------------------------------------

    def register_counter(
        self, name: str, help_text: str, labels: Iterable[str] = (),
    ) -> "_Counter":
        with self._lock:
            self._check_new(name)
            series = _Series(name=name, kind="counter", help_text=help_text)
            self._series[name] = series
        return _Counter(series, tuple(labels))

    def register_gauge(
        self, name: str, help_text: str, labels: Iterable[str] = (),
    ) -> "_Gauge":
        with self._lock:
            self._check_new(name)
            series = _Series(name=name, kind="gauge", help_text=help_text)
            self._series[name] = series
        return _Gauge(series, tuple(labels))

    def register_histogram(
        self,
        name: str,
        help_text: str,
        buckets: Iterable[float] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        labels: Iterable[str] = (),
    ) -> "_Histogram":
        bucket_list = sorted(set(buckets))
        with self._lock:
            self._check_new(name)
            series = _Series(
                name=name,
                kind="histogram",
                help_text=help_text,
                buckets=bucket_list,
            )
            self._series[name] = series
        return _Histogram(series, tuple(labels))

    # -- accessors -------------------------------------------------------

    def get(self, name: str) -> _Series | None:
        return self._series.get(name)

    def series(self) -> list[_Series]:
        return list(self._series.values())

    def _check_new(self, name: str) -> None:
        if name in self._series:
            raise ValueError(f"metric already registered: {name}")


# --------------------------------------------------------------------------- #
# Metric primitives
# --------------------------------------------------------------------------- #


def _render_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return "{" + parts + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class _Counter:
    series: _Series
    label_names: tuple[str, ...]

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        if amount < 0:
            raise ValueError("counter increment must be non-negative")
        self.series.add_sample(amount, labels or {})


@dataclass
class _Gauge:
    series: _Series
    label_names: tuple[str, ...]

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        self.series.add_sample(value, labels or {})

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        self.series.add_sample(amount, labels or {})

    def dec(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        self.series.add_sample(-amount, labels or {})


@dataclass
class _Histogram:
    series: _Series
    label_names: tuple[str, ...]

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        lbls = labels or {}
        self.series.sum_value += value
        self.series.count += 1
        for boundary in self.series.buckets or []:
            if value <= boundary:
                key = float(boundary)
                bucket_lbls = {**lbls, "le": _format_bucket(boundary)}
                self.series.bucket_counts[key] = self.series.bucket_counts.get(key, 0) + 1
                _ = bucket_lbls  # labels are emitted via render; we still bump per-bucket counts.


def _format_bucket(boundary: float) -> str:
    if boundary == float("inf"):
        return "+Inf"
    if boundary >= 1:
        return str(boundary)
    return f"{boundary:g}"


# --------------------------------------------------------------------------- #
# Default registry + helpers
# --------------------------------------------------------------------------- #


_DEFAULT_REGISTRY: MetricRegistry | None = None


def configure_default_registry() -> MetricRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = MetricRegistry()
    return _DEFAULT_REGISTRY


def get_default_registry() -> MetricRegistry:
    return _DEFAULT_REGISTRY or configure_default_registry()


def reset_default_registry() -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


# --------------------------------------------------------------------------- #
# Prometheus text exposition
# --------------------------------------------------------------------------- #


def render_prometheus_text(registry: MetricRegistry | None = None) -> str:
    """Render all registered metrics in Prometheus text exposition format."""
    reg = registry or get_default_registry()
    lines: list[str] = []
    for series in reg.series():
        lines.append(f"# HELP {series.name} {series.help_text}")
        lines.append(f"# TYPE {series.name} {series.kind}")
        if series.kind == "histogram":
            for boundary in series.buckets or []:
                bucket_lbls = {"le": _format_bucket(boundary)}
                count = series.bucket_counts.get(float(boundary), 0)
                lines.append(
                    f'{series.name}_bucket{_render_labels(bucket_lbls)} {count}'
                )
            # +Inf bucket reflects the total observation count.
            lines.append(
                f'{series.name}_bucket{_render_labels({"le": "+Inf"})} {series.count}'
            )
            lines.append(f"{series.name}_sum {series.sum_value}")
            lines.append(f"{series.name}_count {series.count}")
        else:
            # Aggregate samples by labelset for monotonic primitives.
            aggregate: dict[tuple, float] = {}
            for sample in series.samples:
                key = tuple(sorted(sample.labels.items()))
                if series.kind == "counter":
                    aggregate[key] = aggregate.get(key, 0.0) + sample.value
                else:
                    aggregate[key] = sample.value
            for key, value in aggregate.items():
                lbls = dict(key)
                lines.append(
                    f"{series.name}{_render_labels(lbls)} {value}"
                )
    return "\n".join(lines) + ("\n" if lines else "")


__all__ = [
    "MetricRegistry",
    "configure_default_registry",
    "get_default_registry",
    "render_prometheus_text",
    "reset_default_registry",
]
