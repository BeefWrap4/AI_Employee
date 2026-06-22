"""Leader election for the SchedulerLoop (spec §5.8 / §8).

Multi-replica HA: when N agent-platform-api pods run, only one should
tick the cron scheduler — otherwise every due schedule fires N times.

The lease is a Redis ``SET key holder_id NX EX ttl``.  The holder
periodically renews (overwrites the value) before the TTL expires; on
crash, the key expires and another replica acquires it.

When Redis is unavailable (no ``REDIS_URL`` or unreachable),
:class:`LocalLeaderElection` makes every instance believe it's the
leader — correct for single-replica dev/test deployments where there
is no contention.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LeaderLease(Protocol):
    """Contract every leader-election backend satisfies."""

    def try_acquire(self) -> bool: ...
    def renew(self) -> bool: ...
    def is_leader(self) -> bool: ...
    def release(self) -> None: ...


class LocalLeaderElection:
    """No-op lease: always leader.  Used when Redis is unavailable."""

    def try_acquire(self) -> bool:
        return True

    def renew(self) -> bool:
        return True

    def is_leader(self) -> bool:
        return True

    def release(self) -> None:
        return None


class RedisLeaderElection:
    """Redis-backed lease via ``SET NX EX``.

    ``holder_id`` defaults to a per-process UUID so two replicas never
    share an identity.  ``is_leader`` re-reads the key and compares the
    stored value to ``holder_id`` — this catches the case where the
    lease expired and was stolen by another replica between ticks.
    """

    def __init__(
        self,
        *,
        client: Any,
        key: str = "leader:agent-platform:scheduler",
        holder_id: str | None = None,
        ttl_s: int = 15,
    ) -> None:
        self._client = client
        self._key = key
        self._holder_id = holder_id or f"replica-{uuid.uuid4().hex[:8]}"
        self._ttl_s = max(1, int(ttl_s))
        self._acquired = False
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        try:
            ok = self._client.set(self._key, self._holder_id, ex=self._ttl_s, nx=True)
        except Exception as exc:
            logger.warning("leader acquire failed: %s", exc)
            return False
        acquired = bool(ok)
        with self._lock:
            self._acquired = acquired
        return acquired

    def renew(self) -> bool:
        """Refresh the lease.  Only succeeds when we still hold it."""
        if not self.is_leader():
            with self._lock:
                self._acquired = False
            return False
        try:
            # Overwrite with our holder_id and reset TTL.  Use plain SET
            # (not NX) because we've already confirmed ownership via
            # is_leader(); a race here is bounded by the TTL.
            self._client.set(self._key, self._holder_id, ex=self._ttl_s)
        except Exception as exc:
            logger.warning("leader renew failed: %s", exc)
            with self._lock:
                self._acquired = False
            return False
        return True

    def is_leader(self) -> bool:
        try:
            current = self._client.get(self._key)
        except Exception as exc:
            logger.warning("leader get failed: %s", exc)
            return False
        if isinstance(current, bytes):
            current = current.decode("utf-8", errors="replace")
        return current == self._holder_id

    def release(self) -> None:
        """Release the lease so another replica can take over immediately."""
        if not self.is_leader():
            return
        try:
            self._client.delete(self._key)
        except Exception as exc:
            logger.warning("leader release failed: %s", exc)
        finally:
            with self._lock:
                self._acquired = False


def _connect_redis(url: str, *, timeout_s: float) -> Any:
    import redis  # type: ignore[import-not-found]

    return redis.Redis.from_url(url, socket_timeout=timeout_s)


def build_leader_election(
    *,
    redis_url: str | None = None,
    key: str = "leader:agent-platform:scheduler",
    holder_id: str | None = None,
    ttl_s: int = 15,
) -> LeaderLease:
    """Construct a leader-election backend from env.

    Returns :class:`LocalLeaderElection` when ``REDIS_URL`` is unset or
    Redis is unreachable, so single-replica deployments keep working.
    """
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    if not url:
        return LocalLeaderElection()
    try:
        timeout = float(os.environ.get("REDIS_TIMEOUT_S", "0.5"))
        client = _connect_redis(url, timeout_s=timeout)
        client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable for leader election: %s", exc)
        return LocalLeaderElection()
    return RedisLeaderElection(
        client=client,
        key=key,
        holder_id=holder_id,
        ttl_s=ttl_s,
    )


__all__ = [
    "LeaderLease",
    "LocalLeaderElection",
    "RedisLeaderElection",
    "build_leader_election",
]
