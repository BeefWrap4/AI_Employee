"""S3-compatible object-store backend (production / MinIO).

Wraps a boto3 S3 client behind the same :class:`ObjectStore` protocol
as :class:`LocalFsObjectStore`.  Works against AWS S3 as well as
MinIO — both speak the S3 API.

boto3 is imported lazily inside :meth:`__init__` so environments that
only need the LocalFs backend (tests, single-node dev) don't pay the
import cost or need the dependency at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _validate_key(key: str) -> None:
    if not key:
        raise ValueError("object key must not be empty")
    if key.startswith("/") or "\\" in key:
        raise ValueError(f"object key must be relative POSIX path: {key!r}")
    parts = key.split("/")
    if ".." in parts:
        raise ValueError(f"object key must not contain '..': {key!r}")


class S3ObjectStore:
    """S3 / MinIO backend.

    Parameters mirror the env vars consumed by :func:`build_object_store`:

    * ``bucket`` — required.
    * ``access_key`` / ``secret_key`` — credentials (use IAM roles
      / instance profiles in production by passing empty strings).
    * ``endpoint_url`` — for MinIO or VPC endpoints; ``None`` means
      standard AWS.
    * ``region`` — default ``us-east-1``.
    """

    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        access_key: str = "",
        secret_key: str = "",
        endpoint_url: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.client import Config  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "boto3 is required for S3ObjectStore; install with `pip install boto3`",
            ) from exc

        self.bucket = bucket
        self.endpoint_url = endpoint_url or ""

        # MinIO (and any non-AWS S3-compatible endpoint served from a single
        # host without wildcard DNS) requires path-style addressing — i.e.
        # ``http://minio:9000/bucket/key`` rather than the virtual-hosted
        # ``http://bucket.minio:9000/key`` that boto3 picks by default.  The
        # default fails DNS against a real MinIO deployment, so force
        # ``addressing_style="path"`` whenever an explicit endpoint_url is
        # given.  Vanilla AWS S3 (no endpoint_url) keeps virtual-hosted style.
        s3_kwargs: dict[str, Any] = {}
        if endpoint_url:
            s3_kwargs["addressing_style"] = "path"

        kwargs: dict[str, Any] = {
            "region_name": region,
            "config": Config(signature_version="s3v4", s3=s3_kwargs)
            if s3_kwargs
            else Config(signature_version="s3v4"),
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if access_key:
            kwargs["aws_access_key_id"] = access_key
        if secret_key:
            kwargs["aws_secret_access_key"] = secret_key

        # Stash a fresh client per call so boto3 connection pools don't
        # get shared across threads in a way that surprises tests.
        self._boto3 = boto3
        self._client_factory: Callable[[], Any] = lambda: boto3.client("s3", **kwargs)

    # -- operations -------------------------------------------------------- #

    def _client(self):  # type: ignore[no-untyped-def]
        return self._client_factory()

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        _validate_key(key)
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            params["ContentType"] = content_type
        if metadata:
            # boto3 user-metadata keys must be strings and short.
            params["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        self._client().put_object(**params)
        return key

    def get(self, key: str) -> bytes:
        _validate_key(key)
        client = self._client()
        try:
            resp = client.get_object(Bucket=self.bucket, Key=key)
        except client.exceptions.NoSuchKey as exc:  # type: ignore[attr-defined]
            raise KeyError(key) from exc
        except Exception as exc:  # pragma: no cover - depends on boto version
            # boto3 also raises a generic ClientError with code 'NoSuchKey'.
            code = getattr(getattr(exc, "response", {}), "get", lambda *_: None)("Error", {}).get(
                "Code",
            )
            if code in {"NoSuchKey", "404"}:
                raise KeyError(key) from exc
            raise
        body = resp["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def exists(self, key: str) -> bool:
        _validate_key(key)
        client = self._client()
        try:
            client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:  # pragma: no cover - depends on boto version
            code = getattr(getattr(exc, "response", {}), "get", lambda *_: None)(
                "Error",
                {},
            ).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def delete(self, key: str) -> None:
        _validate_key(key)
        self._client().delete_object(Bucket=self.bucket, Key=key)

    def get_metadata(self, key: str) -> dict[str, Any]:
        _validate_key(key)
        client = self._client()
        try:
            resp = client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # pragma: no cover - depends on boto version
            code = getattr(getattr(exc, "response", {}), "get", lambda *_: None)(
                "Error",
                {},
            ).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise KeyError(key) from exc
            raise
        return {
            "content_type": resp.get("ContentType", "application/octet-stream"),
            "size": int(resp.get("ContentLength", 0)),
            "etag": resp.get("ETag", "").strip('"'),
            "metadata": dict((resp.get("Metadata") or {}).items()),
        }

    def presign(self, key: str, *, expires: int = 3600) -> str:
        _validate_key(key)
        client = self._client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )


class MinioObjectStore(S3ObjectStore):
    """Convenience alias for MinIO (S3-compatible).

    MinIO is the S3-compatible store we deploy in dev / staging
    (``docker-compose`` / k8s).  Same code, just clearer intent at the
    call site when the endpoint is ``http://minio:9000``.
    """

    backend = "minio"

    def __init__(
        self,
        *,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str = "http://localhost:9000",
        region: str = "us-east-1",
        secure: bool = False,
    ) -> None:
        # Normalise: boto3 doesn't care about the protocol flag, but
        # accept it so deployment YAML can keep the MinIO docs
        # convention of ``http://`` + ``secure: false``.
        url = endpoint_url
        if not secure and url.startswith("http://"):
            # already http
            pass
        if secure and url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        super().__init__(
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            endpoint_url=url,
            region=region,
        )


__all__ = ["MinioObjectStore", "S3ObjectStore"]
