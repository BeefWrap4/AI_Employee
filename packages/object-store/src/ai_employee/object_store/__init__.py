"""Object-store abstraction (R22).

A minimal protocol for binary object storage so that business code can
run against a local filesystem (dev / single-node / tests) or against
S3-compatible object storage (production / MinIO).  The same
``ObjectStore`` surface lets us move uploads and large attachments
out of the database and out of the local volume without rewriting
callers.

The contract is intentionally small and inspired by common S3 SDKs:

* :meth:`put(key, data, content_type)` — write bytes.
* :meth:`get(key)` — read bytes back (raises :class:`KeyError`).
* :meth:`exists(key)` — boolean probe.
* :meth:`delete(key)` — idempotent remove.
* :meth:`presign(key, expires)` — return a URL the client can use to
  fetch the object directly.  For the LocalFs backend this points at
  the platform API's own ``/api/v1/objects/{key}/download`` endpoint
  since there is no real S3 to sign.
* :meth:`get_metadata(key)` — content-type / size / etag for
  ingestion flows.

The factory :func:`build_object_store` selects an implementation from
env vars so a service can be configured by changing env without code
changes:

* ``OBJECT_STORE_URL`` set → S3 backend (works against AWS S3 or any
  S3-compatible endpoint, including MinIO).
* Unset → :class:`LocalFsObjectStore` writing under
  ``OBJECT_STORE_LOCAL_ROOT`` (default ``./var/objects``).

Credentials come from ``OBJECT_STORE_ACCESS_KEY`` and
``OBJECT_STORE_SECRET_KEY``; ``OBJECT_STORE_BUCKET`` is the default
bucket.  Tests can call :func:`build_object_store` with explicit args.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    """Storage backend for binary objects (uploads, attachments, blobs)."""

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Store ``data`` under ``key`` and return the key.

        ``content_type`` and ``metadata`` are advisory: they round-trip
        through :meth:`get_metadata` for the LocalFs backend and are
        sent as the object's ``ContentType`` / user metadata on S3.
        """
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``.  Raises ``KeyError`` if missing."""
        ...

    def exists(self, key: str) -> bool:
        """True if ``key`` is currently stored."""
        ...

    def delete(self, key: str) -> None:
        """Idempotent remove.  Missing keys do not raise."""
        ...

    def presign(self, key: str, *, expires: int = 3600) -> str:
        """Return a URL the client can use to fetch the object.

        For the LocalFs backend this is a same-origin API URL; for the
        S3 backend it's a real S3 pre-signed URL.
        """
        ...

    def get_metadata(self, key: str) -> dict[str, Any]:
        """Return ``{"content_type": ..., "size": ..., ...}`` for ``key``."""
        ...


# --------------------------------------------------------------------------- #
# Key safety: object keys are user-controlled (uploaded filenames,
# caller-supplied IDs).  Reject anything that would let a caller walk
# out of the object store's root namespace.
# --------------------------------------------------------------------------- #


def _validate_key(key: str) -> None:
    if not key:
        raise ValueError("object key must not be empty")
    if key.startswith("/") or "\\" in key:
        raise ValueError(f"object key must be relative POSIX path: {key!r}")
    # Reject '..' path segments (covers ../foo and foo/../bar).
    parts = key.split("/")
    if ".." in parts:
        raise ValueError(f"object key must not contain '..': {key!r}")


# --------------------------------------------------------------------------- #
# LocalFsObjectStore — default backend (no external dependencies).
# --------------------------------------------------------------------------- #


class LocalFsObjectStore:
    """Filesystem-backed object store.

    Layout::

        {root}/{key}

    Metadata is stored alongside each object as a sidecar JSON file
    (``{key}.meta.json``) so that ``content_type`` and ``size`` survive
    restarts.  ``base_url`` is used by :meth:`presign` to mint same-origin
    download URLs.
    """

    backend = "local"

    def __init__(
        self,
        root: str | Path = "./var/objects",
        *,
        base_url: str = "",
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # ``base_url`` lets the LocalFs backend mint /api/v1/objects/{key}
        # URLs that work behind a reverse proxy.  Default to relative path
        # so dev (no ingress) still works.
        self.base_url = (base_url or "").rstrip("/")

    # -- paths ------------------------------------------------------------- #

    def _path_for(self, key: str) -> Path:
        _validate_key(key)
        return self.root / key

    def _meta_path_for(self, key: str) -> Path:
        return self.root / f"{key}.meta.json"

    # -- operations -------------------------------------------------------- #

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        target = self._path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        meta = {
            "content_type": content_type or "application/octet-stream",
            "size": len(data),
            "metadata": dict(metadata or {}),
        }
        meta_path = self._meta_path_for(key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json

        meta_path.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return key

    def get(self, key: str) -> bytes:
        path = self._path_for(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.is_file():
            path.unlink()
        meta_path = self._meta_path_for(key)
        if meta_path.is_file():
            meta_path.unlink()

    def get_metadata(self, key: str) -> dict[str, Any]:
        import json as _json

        meta_path = self._meta_path_for(key)
        if meta_path.is_file():
            return _json.loads(meta_path.read_text(encoding="utf-8"))
        path = self._path_for(key)
        if not path.is_file():
            raise KeyError(key)
        return {
            "content_type": "application/octet-stream",
            "size": path.stat().st_size,
            "metadata": {},
        }

    def presign(self, key: str, *, expires: int = 3600) -> str:
        """LocalFs has no real signing.  Mint a same-origin API URL.

        The agent-platform-api exposes ``GET /api/v1/objects/{key}/download``
        which streams the bytes back after auth.  Callers should prefer
        that endpoint when running against LocalFs.
        """
        if not self.exists(key):
            raise KeyError(key)
        prefix = self.base_url
        return f"{prefix}/api/v1/objects/{key}" if prefix else f"/api/v1/objects/{key}"


# --------------------------------------------------------------------------- #
# build_object_store factory.
# --------------------------------------------------------------------------- #


def build_object_store(
    *,
    url: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    bucket: str | None = None,
    local_root: str | None = None,
    base_url: str | None = None,
) -> ObjectStore:
    """Pick a backend from explicit args or env vars.

    Env vars consumed:

    * ``OBJECT_STORE_URL`` — when set, the S3 / MinIO backend is used.
      Any http(s) endpoint works (MinIO is S3-compatible).
    * ``OBJECT_STORE_ACCESS_KEY`` / ``OBJECT_STORE_SECRET_KEY`` —
      credentials for the S3-compatible backend.
    * ``OBJECT_STORE_BUCKET`` — default bucket.
    * ``OBJECT_STORE_LOCAL_ROOT`` — root directory for the LocalFs
      fallback (default ``./var/objects``).
    """
    chosen_url = url if url is not None else os.environ.get("OBJECT_STORE_URL")
    if chosen_url:
        # S3 / MinIO branch — only imported lazily so LocalFs-only
        # environments don't need boto3 installed.
        from ai_employee.object_store.s3 import (  # noqa: WPS433 (lazy import)
            S3ObjectStore,
        )

        chosen_bucket = bucket or os.environ.get("OBJECT_STORE_BUCKET") or "ai-employee"
        chosen_access = access_key if access_key is not None else os.environ.get(
            "OBJECT_STORE_ACCESS_KEY",
            "",
        )
        chosen_secret = secret_key if secret_key is not None else os.environ.get(
            "OBJECT_STORE_SECRET_KEY",
            "",
        )
        # MinIO endpoints normally need path-style addressing; boto3
        # auto-detects from the URL, but we make it explicit so that
        # production deployments with virtual-hosted buckets keep
        # working.
        return S3ObjectStore(
            bucket=chosen_bucket,
            access_key=chosen_access,
            secret_key=chosen_secret,
            endpoint_url=chosen_url,
        )
    chosen_root = local_root or os.environ.get("OBJECT_STORE_LOCAL_ROOT", "./var/objects")
    chosen_base = base_url if base_url is not None else os.environ.get(
        "OBJECT_STORE_BASE_URL",
        "",
    )
    return LocalFsObjectStore(root=chosen_root, base_url=chosen_base)


__all__ = [
    "LocalFsObjectStore",
    "ObjectStore",
    "build_object_store",
]