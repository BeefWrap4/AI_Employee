"""Vault secret management tests (spec §8 / P3 治理).

The :class:`SecretResolver` reads secrets from Vault when configured
and falls back to env vars (dev) when Vault is unavailable.  This keeps
local dev zero-config while production pulls all credentials from
Vault — no plaintext secrets in env or images.
"""
from __future__ import annotations

import pytest

from ai_employee.common_schemas.secrets import (
    EnvFallbackResolver,
    SecretResolutionError,
    SecretResolver,
    VaultSecretResolver,
    build_secret_resolver,
)


# --------------------------------------------------------------------------- #
# EnvFallbackResolver (dev default)
# --------------------------------------------------------------------------- #


def test_env_resolver_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "env-pw")
    resolver = EnvFallbackResolver()
    assert resolver.get("db/password", env_var="DB_PASSWORD") == "env-pw"


def test_env_resolver_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_PW", raising=False)
    resolver = EnvFallbackResolver()
    with pytest.raises(SecretResolutionError):
        resolver.get("db/password", env_var="MISSING_PW")


def test_env_resolver_returns_default_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT", raising=False)
    resolver = EnvFallbackResolver()
    assert resolver.get("db/password", env_var="ABSENT", default="fallback") == "fallback"


# --------------------------------------------------------------------------- #
# VaultSecretResolver with a fake hvac client
# --------------------------------------------------------------------------- #


class _FakeVaultClient:
    """Minimal fake of hvac.Client — only implements read+is_authenticated."""

    def __init__(self, secrets: dict[str, dict[str, str]] | None = None, *, authed: bool = True) -> None:
        self._secrets = secrets or {}
        self._authed = authed
        self.read_calls: list[str] = []

    def is_authenticated(self) -> bool:
        return self._authed

    def read(self, path: str) -> dict | None:
        self.read_calls.append(path)
        return self._secrets.get(path)

    def secrets(self, *additions: tuple[str, str, str]) -> "_FakeVaultClient":
        for path, key, value in additions:
            self._secrets[path] = {"data": {"data": {key: value}}}
        return self


def test_vault_resolver_reads_secret() -> None:
    fake = _FakeVaultClient().secrets(
        ("secret/data/ai_employee/db", "password", "vault-pw"),
    )
    resolver = VaultSecretResolver(client=fake)  # type: ignore[arg-type]
    assert resolver.get("db/password") == "vault-pw"
    assert "secret/data/ai_employee/db" in fake.read_calls


def test_vault_resolver_unauthenticated_raises() -> None:
    fake = _FakeVaultClient(authed=False)
    resolver = VaultSecretResolver(client=fake, allow_env_fallback=False)  # type: ignore[arg-type]
    with pytest.raises(SecretResolutionError, match="not authenticated"):
        resolver.get("db/password")


def test_vault_resolver_missing_path_raises() -> None:
    fake = _FakeVaultClient()  # no secrets configured
    resolver = VaultSecretResolver(client=fake, allow_env_fallback=False)  # type: ignore[arg-type]
    with pytest.raises(SecretResolutionError, match="not found"):
        resolver.get("db/password")


def test_vault_resolver_falls_back_to_env_when_vault_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Vault has no secret at the path, fall back to env (dev)."""
    monkeypatch.setenv("DB_PASSWORD", "env-pw")
    fake = _FakeVaultClient()  # empty vault
    resolver = VaultSecretResolver(client=fake, allow_env_fallback=True)  # type: ignore[arg-type]
    assert resolver.get("db/password", env_var="DB_PASSWORD") == "env-pw"


def test_vault_resolver_no_fallback_raises() -> None:
    fake = _FakeVaultClient()
    resolver = VaultSecretResolver(client=fake, allow_env_fallback=False)  # type: ignore[arg-type]
    with pytest.raises(SecretResolutionError):
        resolver.get("db/password", env_var="DB_PASSWORD")


def test_vault_resolver_caches_secret() -> None:
    fake = _FakeVaultClient().secrets(
        ("secret/data/ai_employee/db", "password", "vault-pw"),
    )
    resolver = VaultSecretResolver(client=fake)  # type: ignore[arg-type]
    resolver.get("db/password")
    resolver.get("db/password")
    # Second read served from cache.
    assert fake.read_calls.count("secret/data/ai_employee/db") == 1


# --------------------------------------------------------------------------- #
# build_secret_resolver
# --------------------------------------------------------------------------- #


def test_build_resolver_defaults_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    resolver = build_secret_resolver()
    assert isinstance(resolver, EnvFallbackResolver)


def test_build_resolver_uses_vault_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://127.0.0.1:8200")
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")

    # Stub hvac.Client so build_secret_resolver doesn't need a live Vault.
    import ai_employee.common_schemas.secrets as secrets_mod

    class _AuthedClient:
        def is_authenticated(self) -> bool:
            return True

        def read(self, path):
            return None

    def fake_connect(addr, token, *, timeout_s):
        return _AuthedClient()

    monkeypatch.setattr(secrets_mod, "_connect_vault", fake_connect)
    resolver = build_secret_resolver()
    assert isinstance(resolver, VaultSecretResolver)


def test_build_resolver_vault_unreachable_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://127.0.0.1:1")  # unreachable
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    monkeypatch.setenv("VAULT_TIMEOUT_S", "0.2")
    resolver = build_secret_resolver()
    # Falls back to env resolver so dev keeps working.
    assert isinstance(resolver, EnvFallbackResolver)


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


def test_resolvers_share_protocol() -> None:
    a: SecretResolver = EnvFallbackResolver()
    b: SecretResolver = VaultSecretResolver(client=_FakeVaultClient().secrets(  # type: ignore[arg-type]
        ("secret/data/ai_employee/x", "k", "v"),
    ))
    assert hasattr(a, "get") and hasattr(b, "get")


def test_secret_resolution_error_message() -> None:
    err = SecretResolutionError("boom", path="db/password")
    assert "db/password" in str(err)
    assert err.path == "db/password"
