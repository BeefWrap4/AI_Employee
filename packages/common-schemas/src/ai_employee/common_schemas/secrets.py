"""Vault / secret management (spec §8 / P3 治理).

Centralises credential access behind a :class:`SecretResolver` so
business code never touches ``os.environ`` for secrets directly:

* :class:`VaultSecretResolver` — reads from HashiCorp Vault via hvac.
  Secrets are cached per-path.  When Vault is unreachable or the path
  is missing, it can fall back to an env var (configurable) so dev
  keeps working without a Vault server.
* :class:`EnvFallbackResolver` — dev default; reads plain env vars.

:func:`build_secret_resolver` picks one from env: when ``VAULT_ADDR``
is set and reachable, Vault wins; otherwise env fallback.  This means
production images carry no plaintext secrets (everything in Vault) and
local dev needs only env vars.

Vault KV-v2 paths are used: ``secret/data/<mount>/<key>``.  The
``get("db/password")`` call maps to path
``secret/data/ai_employee/db`` with data key ``password``.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SecretResolutionError(RuntimeError):
    """Raised when a secret cannot be resolved from any source."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        full = f"{message} (path={path})" if path else message
        super().__init__(full)
        self.path = path


class SecretResolver(Protocol):
    """Contract every secret backend satisfies."""

    def get(
        self,
        path: str,
        *,
        env_var: str | None = None,
        default: str | None = None,
    ) -> str: ...


class EnvFallbackResolver:
    """Reads secrets from environment variables (dev default)."""

    def get(
        self,
        path: str,
        *,
        env_var: str | None = None,
        default: str | None = None,
    ) -> str:
        if env_var is None:
            env_var = path.replace("/", "_").upper()
        value = os.environ.get(env_var)
        if value is None or value == "":
            if default is not None:
                return default
            raise SecretResolutionError(
                f"secret {path!r} not in env ({env_var})", path=path,
            )
        return value


class VaultSecretResolver:
    """Reads secrets from HashiCorp Vault (KV-v2) with caching.

    Falls back to :class:`EnvFallbackResolver` when Vault has no secret
    at the path AND ``allow_env_fallback=True`` — useful during
    migration so a partially-populated Vault doesn't break boot.
    """

    def __init__(
        self,
        *,
        client: Any,
        mount: str = "ai_employee",
        allow_env_fallback: bool = True,
    ) -> None:
        self._client = client
        self._mount = mount
        self._allow_env_fallback = allow_env_fallback
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._env_fallback = EnvFallbackResolver()

    def _vault_path(self, path: str) -> tuple[str, str]:
        """Map ``db/password`` → (``secret/data/ai_employee/db``, ``password``)."""
        if "/" not in path:
            raise SecretResolutionError(
                f"secret path must be '<resource>/<key>', got {path!r}", path=path,
            )
        resource, key = path.split("/", 1)
        return f"secret/data/{self._mount}/{resource}", key

    def get(
        self,
        path: str,
        *,
        env_var: str | None = None,
        default: str | None = None,
    ) -> str:
        with self._lock:
            if path in self._cache:
                return self._cache[path]

        if not self._client.is_authenticated():
            if self._allow_env_fallback:
                return self._env_fallback.get(path, env_var=env_var, default=default)
            raise SecretResolutionError(
                f"Vault not authenticated for {path!r}", path=path,
            )

        vpath, key = self._vault_path(path)
        try:
            result = self._client.read(vpath)
        except Exception as exc:
            logger.warning("Vault read failed for %s: %s", vpath, exc)
            if self._allow_env_fallback:
                return self._env_fallback.get(path, env_var=env_var, default=default)
            raise SecretResolutionError(
                f"Vault read failed for {path!r}: {exc}", path=path,
            ) from exc

        if result is None:
            if self._allow_env_fallback:
                return self._env_fallback.get(path, env_var=env_var, default=default)
            raise SecretResolutionError(
                f"secret {path!r} not found in Vault at {vpath}", path=path,
            )

        # KV-v2: {"data": {"data": {key: value}}}
        data = result.get("data", {}).get("data", {}) if isinstance(result, dict) else {}
        value = data.get(key)
        if value is None:
            if default is not None:
                return default
            raise SecretResolutionError(
                f"Vault secret {path!r} has no key {key!r}", path=path,
            )
        with self._lock:
            self._cache[path] = value
        return value


def _connect_vault(addr: str, token: str, *, timeout_s: float) -> Any:
    try:
        import hvac  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "hvac is required for VaultSecretResolver; install with `pip install hvac`",
        ) from exc
    client = hvac.Client(url=addr, token=token, timeout=timeout_s)
    return client


def build_secret_resolver(
    *,
    vault_addr: str | None = None,
    vault_token: str | None = None,
) -> SecretResolver:
    """Build a resolver from env.  Vault wins when reachable; else env.

    Env: ``VAULT_ADDR``, ``VAULT_TOKEN``, ``VAULT_TIMEOUT_S``.
    """
    addr = vault_addr if vault_addr is not None else os.environ.get("VAULT_ADDR")
    token = vault_token if vault_token is not None else os.environ.get("VAULT_TOKEN")
    if not addr or not token:
        return EnvFallbackResolver()
    try:
        timeout = float(os.environ.get("VAULT_TIMEOUT_S", "1.0"))
        client = _connect_vault(addr, token, timeout_s=timeout)
        if not client.is_authenticated():
            logger.warning("Vault at %s not authenticated; falling back to env", addr)
            return EnvFallbackResolver()
        return VaultSecretResolver(client=client, allow_env_fallback=True)
    except Exception as exc:
        logger.warning("Vault unavailable (%s); falling back to env resolver: %s", addr, exc)
        return EnvFallbackResolver()


__all__ = [
    "EnvFallbackResolver",
    "SecretResolutionError",
    "SecretResolver",
    "VaultSecretResolver",
    "build_secret_resolver",
]
