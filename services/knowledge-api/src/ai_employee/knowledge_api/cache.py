"""Redis-backed query cache + embedding cache for knowledge-api.

Both caches follow the same shape: a typed ``get``/``set`` interface, an
in-process no-op fallback, and lazy Redis import so the module loads in
environments where the dependency is unavailable.

When ``REDIS_URL`` is unset, ``build_query_cache`` / ``build_embedding_cache``
return a :class:`NoOpCache`.  When Redis is set but unreachable (connection
refused, auth error, …), the factories catch the exception and degrade to
the same no-op cache so a flaky cache never breaks an API call.

Cache keys are sha256 hex digests — see :func:`query_cache_key` and
:func:`embedding_cache_key` for the canonical inputs.  Stable keys mean
identical requests hit the same cache slot even across processes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Key helpers
# --------------------------------------------------------------------------- #


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def query_cache_key(
    *,
    question: str,
    acl_tags: list[str],
    template_id: str,
    top_k: int,
) -> str:
    """Stable cache key for a knowledge query.

    The key incorporates every dimension the answer depends on so two
    different ACL scopes or top-K values cannot collide.
    """
    payload = json.dumps(
        {
            "q": question.strip(),
            "acl": sorted(acl_tags),
            "tid": template_id,
            "k": top_k,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _sha256_hex(f"qcache:{payload}")


def embedding_cache_key(text: str) -> str:
    """Stable cache key for a chunk / query embedding."""
    return _sha256_hex(f"emb:{text.strip()}")


# --------------------------------------------------------------------------- #
# Cache protocol
# --------------------------------------------------------------------------- #


class _CacheBackend(Protocol):
    """Minimal interface both Redis and the no-op cache satisfy."""

    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any, ttl: int) -> None: ...


class NoOpCache:
    """In-process no-op cache used when Redis is unavailable."""

    enabled: bool = True

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled

    def get(self, key: str) -> Any:
        return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        return None


# --------------------------------------------------------------------------- #
# Query cache
# --------------------------------------------------------------------------- #


@dataclass
class QueryCache:
    """Stores JSON-serialisable query answers (dicts / lists)."""

    redis_client: Any = None
    default_ttl_s: int = 300
    enabled: bool = True

    def get(self, key: str) -> Any:
        if not self.enabled or self.redis_client is None:
            return None
        try:
            raw = self.redis_client.get(key)
        except Exception as exc:
            logger.warning("query cache get failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if not self.enabled or self.redis_client is None:
            return
        try:
            self.redis_client.set(
                key, json.dumps(value, ensure_ascii=False),
                ex=ttl if ttl is not None else self.default_ttl_s,
            )
        except Exception as exc:
            logger.warning("query cache set failed: %s", exc)


@dataclass
class EmbeddingCache:
    """Stores float-vector embeddings (JSON-serialised as lists)."""

    redis_client: Any = None
    default_ttl_s: int = 3600
    enabled: bool = True

    def get(self, key: str) -> list[float] | None:
        if not self.enabled or self.redis_client is None:
            return None
        try:
            raw = self.redis_client.get(key)
        except Exception as exc:
            logger.warning("embedding cache get failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return list(value) if isinstance(value, list) else None

    def set(self, key: str, value: list[float], ttl: int | None = None) -> None:
        if not self.enabled or self.redis_client is None:
            return
        try:
            self.redis_client.set(
                key, json.dumps(list(value)),
                ex=ttl if ttl is not None else self.default_ttl_s,
            )
        except Exception as exc:
            logger.warning("embedding cache set failed: %s", exc)


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #


def _connect_redis(url: str, *, timeout_s: float) -> Any:
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "redis is required for the Redis cache backend; "
            "install with `pip install redis`",
        ) from exc
    return redis.Redis.from_url(url, socket_timeout=timeout_s, decode_responses=True)


def build_query_cache(
    *,
    redis_url: str | None = None,
    default_ttl_s: int | None = None,
    enabled: bool | None = None,
) -> _CacheBackend:
    """Construct a :class:`QueryCache` or :class:`NoOpCache` from env.

    Disabled / degraded when ``REDIS_URL`` is unset or Redis is unreachable.
    """
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    if not url:
        return NoOpCache(disabled=True)
    try:
        timeout = float(os.environ.get("REDIS_TIMEOUT_S", "0.5"))
        client = _connect_redis(url, timeout_s=timeout)
        # Round-trip probe; raises on unreachable.
        client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable for query cache: %s", exc)
        return NoOpCache(disabled=True)
    return QueryCache(
        redis_client=client,
        default_ttl_s=default_ttl_s if default_ttl_s is not None else int(
            os.environ.get("QUERY_CACHE_TTL_S", "300")
        ),
        enabled=bool(enabled) if enabled is not None else True,
    )


def build_embedding_cache(
    *,
    redis_url: str | None = None,
    default_ttl_s: int | None = None,
    enabled: bool | None = None,
) -> _CacheBackend:
    """Construct a :class:`EmbeddingCache` or :class:`NoOpCache` from env."""
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    if not url:
        return NoOpCache(disabled=True)
    try:
        timeout = float(os.environ.get("REDIS_TIMEOUT_S", "0.5"))
        client = _connect_redis(url, timeout_s=timeout)
        client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable for embedding cache: %s", exc)
        return NoOpCache(disabled=True)
    return EmbeddingCache(
        redis_client=client,
        default_ttl_s=default_ttl_s if default_ttl_s is not None else int(
            os.environ.get("EMBEDDING_CACHE_TTL_S", "3600")
        ),
        enabled=bool(enabled) if enabled is not None else True,
    )


__all__ = [
    "EmbeddingCache",
    "NoOpCache",
    "QueryCache",
    "build_embedding_cache",
    "build_query_cache",
    "embedding_cache_key",
    "query_cache_key",
]
