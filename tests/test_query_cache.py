"""Query cache + embedding cache tests (Redis-backed with no-op fallback)."""
from __future__ import annotations

import time

import pytest
from ai_employee.knowledge_api.cache import (
    EmbeddingCache,
    NoOpCache,
    QueryCache,
    build_embedding_cache,
    build_query_cache,
    embedding_cache_key,
    query_cache_key,
)

# --------------------------------------------------------------------------- #
# key helpers
# --------------------------------------------------------------------------- #


def test_query_cache_key_includes_all_dimensions() -> None:
    base = query_cache_key(
        question="什么是 RRC 建立失败？",
        acl_tags=["public", "ops"],
        template_id="knowledge_qa",
        top_k=5,
    )
    other_q = query_cache_key(
        question="什么是 RRC 建立失败？",
        acl_tags=["public", "ops"],
        template_id="knowledge_qa",
        top_k=5,
    )
    assert base == other_q  # deterministic
    assert len(base) == 64  # sha256 hex


def test_query_cache_key_changes_with_question() -> None:
    a = query_cache_key(
        question="什么是 RRC 建立失败？", acl_tags=["public"],
        template_id="knowledge_qa", top_k=5,
    )
    b = query_cache_key(
        question="什么是 PRB 利用率？", acl_tags=["public"],
        template_id="knowledge_qa", top_k=5,
    )
    assert a != b


def test_query_cache_key_changes_with_acl_tags() -> None:
    a = query_cache_key(
        question="x", acl_tags=["public"],
        template_id="knowledge_qa", top_k=5,
    )
    b = query_cache_key(
        question="x", acl_tags=["internal"],
        template_id="knowledge_qa", top_k=5,
    )
    assert a != b


def test_query_cache_key_changes_with_top_k() -> None:
    a = query_cache_key(
        question="x", acl_tags=["public"],
        template_id="knowledge_qa", top_k=5,
    )
    b = query_cache_key(
        question="x", acl_tags=["public"],
        template_id="knowledge_qa", top_k=10,
    )
    assert a != b


def test_embedding_cache_key_deterministic() -> None:
    a = embedding_cache_key("hello world")
    b = embedding_cache_key("hello world")
    assert a == b
    assert len(a) == 64


def test_embedding_cache_key_changes_with_text() -> None:
    assert embedding_cache_key("a") != embedding_cache_key("b")


# --------------------------------------------------------------------------- #
# NoOpCache fallback
# --------------------------------------------------------------------------- #


def test_noop_cache_get_returns_none() -> None:
    cache = NoOpCache()
    assert cache.get("anything") is None


def test_noop_cache_set_is_silent() -> None:
    cache = NoOpCache()
    cache.set("k", {"value": 1}, ttl=60)
    assert cache.get("k") is None


def test_noop_cache_disabled_returns_none() -> None:
    cache = NoOpCache(disabled=True)
    cache.set("k", "v", ttl=60)
    assert cache.get("k") is None


# --------------------------------------------------------------------------- #
# build_query_cache / build_embedding_cache
# --------------------------------------------------------------------------- #


def test_build_query_cache_no_redis_returns_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    cache = build_query_cache()
    assert isinstance(cache, NoOpCache)


def test_build_query_cache_unreachable_redis_returns_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # unreachable port
    monkeypatch.setenv("REDIS_TIMEOUT_S", "0.1")
    cache = build_query_cache()
    assert isinstance(cache, NoOpCache)


def test_build_embedding_cache_no_redis_returns_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    cache = build_embedding_cache()
    assert isinstance(cache, NoOpCache)


# --------------------------------------------------------------------------- #
# In-process fake-redis (skipped if redis isn't installed locally)
# --------------------------------------------------------------------------- #


class _FakeRedis:
    """Minimal dict-based fake for unit tests (no real Redis needed)."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> bytes | None:
        item = self.store.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at and expires_at < time.time():
            self.store.pop(key, None)
            return None
        return value.encode("utf-8")

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        expires = time.time() + ex if ex else 0.0
        self.store[key] = (value, expires)


def test_query_cache_round_trip_via_fake_redis() -> None:
    fake = _FakeRedis()
    cache = QueryCache(redis_client=fake, default_ttl_s=60, enabled=True)  # type: ignore[arg-type]
    cache.set("k1", {"hits": 3}, ttl=60)
    assert cache.get("k1") == {"hits": 3}


def test_query_cache_ttl_expires() -> None:
    fake = _FakeRedis()
    cache = QueryCache(redis_client=fake, default_ttl_s=1, enabled=True)  # type: ignore[arg-type]
    # Manually insert an already-expired entry to simulate TTL elapse.
    fake.store["k1"] = ("v1", time.time() - 5)
    assert cache.get("k1") is None


def test_embedding_cache_round_trip() -> None:
    fake = _FakeRedis()
    cache = EmbeddingCache(redis_client=fake, default_ttl_s=3600, enabled=True)  # type: ignore[arg-type]
    vec = [0.1, 0.2, 0.3]
    cache.set("hello", vec, ttl=3600)
    assert cache.get("hello") == vec


def test_query_cache_disabled_is_noop() -> None:
    fake = _FakeRedis()
    cache = QueryCache(redis_client=fake, default_ttl_s=60, enabled=False)
    cache.set("k", "v", ttl=60)
    assert cache.get("k") is None
    # Should not have hit the fake either.
    assert fake.store == {}
