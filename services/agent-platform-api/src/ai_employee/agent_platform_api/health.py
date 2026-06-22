"""Readiness vs liveness probe helpers (spec §6.3).

Splitting the two probes is a production requirement:

* **Liveness** (``/health``) — "the process can serve at all". Cheap,
  always 200 unless the process is shutting down.  k8s uses this to
  decide whether to *restart* the pod.
* **Readiness** (``/health/ready``) — "downstream deps are reachable
  and the service can serve *traffic*".  Runs the dependency checks;
  returns 503 when any one is unhealthy so k8s stops routing traffic
  to this pod without restarting it.

Each dependency is a :class:`DependencyCheck` with a name, health
flag, measured latency, and optional error string.  A
:class:`ReadinessResult` aggregates them and exposes a
:meth:`to_dict` for the JSON body.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class DependencyCheck:
    """Outcome of probing one downstream dependency."""

    name: str
    healthy: bool
    latency_ms: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReadinessResult:
    """Aggregate of all dependency checks for one readiness probe."""

    checks: list[DependencyCheck] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.healthy for c in self.checks)

    @property
    def unhealthy(self) -> list[str]:
        return [c.name for c in self.checks if not c.healthy]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [c.to_dict() for c in self.checks],
            "unhealthy": self.unhealthy,
        }


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - started) * 1000.0


def check_sqlite(db_path: str) -> DependencyCheck:
    """Probe a SQLite database by running ``SELECT 1``.

    Returns a :class:`DependencyCheck` with ``healthy=False`` when the
    file is missing or the query fails.  The file-existence check is
    explicit because ``sqlite3.connect`` auto-creates the file, which
    would mask a missing-DB condition.
    """
    import os

    def probe() -> None:
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"sqlite db not found: {db_path}")
        conn = sqlite3.connect(db_path, timeout=1.0)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()

    try:
        _, latency_ms = _timed(probe)
    except Exception as exc:
        return DependencyCheck(
            name="sqlite",
            healthy=False,
            latency_ms=0.0,
            error=str(exc),
        )
    return DependencyCheck(name="sqlite", healthy=True, latency_ms=round(latency_ms, 3))


def check_redis(url: str, *, timeout_s: float = 0.5) -> DependencyCheck:
    """Probe a Redis instance with ``PING``.

    Degrades to ``healthy=False`` when Redis is unreachable or the
    ``redis`` package isn't installed.
    """
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as exc:
        return DependencyCheck(
            name="redis",
            healthy=False,
            latency_ms=0.0,
            error=f"redis not installed: {exc}",
        )

    def probe() -> None:
        client = redis.Redis.from_url(url, socket_timeout=timeout_s)
        try:
            client.ping()
        finally:
            client.close()

    try:
        _, latency_ms = _timed(probe)
    except Exception as exc:
        return DependencyCheck(
            name="redis",
            healthy=False,
            latency_ms=0.0,
            error=str(exc),
        )
    return DependencyCheck(name="redis", healthy=True, latency_ms=round(latency_ms, 3))


def check_http(url: str, *, timeout_s: float = 1.0) -> DependencyCheck:
    """Probe a downstream HTTP service with a HEAD request."""
    import httpx

    def probe() -> None:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.head(url)
            if resp.status_code >= 500:
                raise RuntimeError(f"downstream returned {resp.status_code}")

    try:
        _, latency_ms = _timed(probe)
    except Exception as exc:
        return DependencyCheck(
            name="http",
            healthy=False,
            latency_ms=0.0,
            error=str(exc),
        )
    return DependencyCheck(name="http", healthy=True, latency_ms=round(latency_ms, 3))


__all__ = [
    "DependencyCheck",
    "ReadinessResult",
    "check_http",
    "check_redis",
    "check_sqlite",
]
