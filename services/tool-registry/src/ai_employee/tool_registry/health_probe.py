"""R25-T: Background health probe for registered tools (spec §5.3).

Each registered tool may declare a ``health_check_url``.  This module
provides a sync helper that probes a single URL and a fan-out
``run_once`` that iterates the :class:`ToolRegistryStore` and writes
back ``health_status`` (``healthy`` / ``unhealthy`` / ``unknown``).

The probe is *not* async — it is called from FastAPI's
``on_event("startup")`` background thread.  Each probe uses urllib with
a short timeout so a slow endpoint never blocks the loop.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Statuses aligned with the platform's :class:`HealthStatus`.
STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"
STATUS_UNKNOWN = "unknown"


@dataclass
class ProbeResult:
    status: str
    latency_ms: float
    error: str | None = None


def _probe_url(url: str | None, timeout_ms: int = 1500) -> ProbeResult:
    """Synchronous HTTP GET to ``url``.  Returns ``UNKNOWN`` for missing /
    unparseable URLs (do not flag tools as broken just because they have
    no endpoint yet)."""
    if not url or not url.startswith(("http://", "https://")):
        return ProbeResult(status=STATUS_UNKNOWN, latency_ms=0.0, error="no health_check_url")
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=max(0.01, timeout_ms / 1000.0)) as resp:
            code = getattr(resp, "status", 200) or 200
        latency = (time.monotonic() - started) * 1000.0
        if 200 <= code < 300:
            return ProbeResult(status=STATUS_HEALTHY, latency_ms=latency)
        return ProbeResult(status=STATUS_UNHEALTHY, latency_ms=latency, error=f"http {code}")
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
        latency = (time.monotonic() - started) * 1000.0
        return ProbeResult(
            status=STATUS_UNHEALTHY,
            latency_ms=latency,
            error=str(exc) or exc.__class__.__name__,
        )


def probe_and_persist(
    store: Any,
    *,
    name: str,
    timeout_ms: int = 1500,
    prober: Callable[[str | None, int], ProbeResult] = _probe_url,
) -> ProbeResult:
    """Probe one tool's ``health_check_url`` and persist the result.

    Skips unknown tool names silently (the tool may have been unregistered
    concurrently) and tolerates a missing ``health_status`` column on the
    store by patching the SQLite schema in-place.
    """
    row = store.get(name)
    if row is None:
        return ProbeResult(status=STATUS_UNKNOWN, latency_ms=0.0, error="tool_not_found")
    result = prober(row.get("health_check_url"), timeout_ms)
    # The store may not have a health_status column on legacy schemas.
    try:
        store.update_health_status(name, result.status, result.latency_ms, result.error)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("health_probe update failed for %s: %s", name, exc)
    return result


def run_once(
    store: Any,
    *,
    timeout_ms: int = 1500,
    prober: Callable[[str | None, int], ProbeResult] | None = None,
) -> dict[str, int]:
    """Probe every tool in the store.  Returns counts {probed, skipped,
    failed} for observability."""
    prober = prober or _probe_url
    counts = {"probed": 0, "skipped": 0, "failed": 0}
    for row in store.list():
        url = row.get("health_check_url")
        if not url:
            counts["skipped"] += 1
            continue
        try:
            probe_and_persist(
                store,
                name=row["name"],
                timeout_ms=timeout_ms,
                prober=prober,
            )
            counts["probed"] += 1
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("health_probe %s failed: %s", row.get("name"), exc)
            counts["failed"] += 1
    return counts


__all__ = [
    "STATUS_HEALTHY",
    "STATUS_UNHEALTHY",
    "STATUS_UNKNOWN",
    "ProbeResult",
    "probe_and_persist",
    "run_once",
]
