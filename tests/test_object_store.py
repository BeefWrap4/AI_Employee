"""Tests for the object-store abstraction.

These tests pin the LocalFs backend (default in dev/test) and the S3
backend (production).  The S3 backend is exercised via moto so we
don't need a real MinIO instance in CI.

The factory test asserts that ``build_object_store`` picks the right
backend purely from env vars, which is what services rely on at
import time.
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest import mock

import pytest
from ai_employee.object_store import (
    LocalFsObjectStore,
    ObjectStore,
    build_object_store,
)


# --------------------------------------------------------------------------- #
# LocalFsObjectStore (default backend; always available).
# --------------------------------------------------------------------------- #


def test_local_fs_put_get_roundtrip(tmp_path: Path) -> None:
    store = LocalFsObjectStore(root=str(tmp_path / "objects"))
    store.put("docs/foo.pdf", b"hello world", content_type="application/pdf")
    assert store.get("docs/foo.pdf") == b"hello world"
    assert store.exists("docs/foo.pdf") is True
    assert store.get_metadata("docs/foo.pdf")["content_type"] == "application/pdf"


def test_local_fs_exists_false_for_missing(tmp_path: Path) -> None:
    store = LocalFsObjectStore(root=str(tmp_path / "objects"))
    assert store.exists("nope.txt") is False
    with pytest.raises(KeyError):
        store.get("nope.txt")


def test_local_fs_delete_removes_object(tmp_path: Path) -> None:
    store = LocalFsObjectStore(root=str(tmp_path / "objects"))
    store.put("a/b.bin", b"payload")
    assert store.exists("a/b.bin")
    store.delete("a/b.bin")
    assert not store.exists("a/b.bin")


def test_local_fs_presign_returns_file_uri(tmp_path: Path) -> None:
    store = LocalFsObjectStore(root=str(tmp_path / "objects"), base_url="http://localhost")
    store.put("docs/x.pdf", b"x")
    url = store.presign("docs/x.pdf", expires=60)
    # LocalFs has no real signing: callers can stream via /api/v1/objects/{key}.
    assert url == "http://localhost/api/v1/objects/docs/x.pdf"


def test_local_fs_rejects_absolute_or_traversal_keys(tmp_path: Path) -> None:
    store = LocalFsObjectStore(root=str(tmp_path / "objects"))
    with pytest.raises(ValueError):
        store.put("/etc/passwd", b"x")
    with pytest.raises(ValueError):
        store.put("../escape.txt", b"x")


def test_local_fs_implements_protocol(tmp_path: Path) -> None:
    store = LocalFsObjectStore(root=str(tmp_path / "objects"))
    # Static check: satisfies the ObjectStore protocol.
    assert isinstance(store, ObjectStore)


# --------------------------------------------------------------------------- #
# build_object_store factory.
# --------------------------------------------------------------------------- #


def test_factory_defaults_to_local_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBJECT_STORE_URL", raising=False)
    monkeypatch.delenv("OBJECT_STORE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("OBJECT_STORE_SECRET_KEY", raising=False)
    monkeypatch.delenv("OBJECT_STORE_BUCKET", raising=False)
    monkeypatch.setenv("OBJECT_STORE_LOCAL_ROOT", str(tmp_path / "default"))
    store = build_object_store()
    assert isinstance(store, LocalFsObjectStore)


def test_factory_with_minio_url_uses_s3_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MinIO is S3-compatible; an http endpoint triggers the S3 backend."""
    monkeypatch.setenv("OBJECT_STORE_URL", "http://minio:9000")
    monkeypatch.setenv("OBJECT_STORE_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("OBJECT_STORE_SECRET_KEY", "minioadmin")
    monkeypatch.setenv("OBJECT_STORE_BUCKET", "ai-employee")
    store = build_object_store()
    # Without moto / boto3 client wiring, ensure the backend advertises
    # S3 semantics (bucket, endpoint) and isn't the LocalFs fallback.
    assert not isinstance(store, LocalFsObjectStore)
    assert getattr(store, "bucket", None) == "ai-employee"
    assert getattr(store, "endpoint_url", "") == "http://minio:9000"


# --------------------------------------------------------------------------- #
# S3 backend (production / MinIO) — exercised via moto so no network needed.
# --------------------------------------------------------------------------- #


@pytest.fixture
def s3_store(monkeypatch: pytest.MonkeyPatch):
    # Lazy-import the boto3-touching module so the test runs even if the
    # package is installed without boto3 (LocalFs-only environments).
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    from ai_employee.object_store.s3 import S3ObjectStore

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    mock_aws = moto.mock_aws()
    mock_aws.start()
    try:
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        store = S3ObjectStore(
            bucket="test-bucket",
            access_key="testing",
            secret_key="testing",
            region="us-east-1",
        )
        yield store
    finally:
        mock_aws.stop()


def test_s3_put_get_roundtrip(s3_store) -> None:
    s3_store.put("docs/foo.pdf", b"hello", content_type="application/pdf")
    assert s3_store.get("docs/foo.pdf") == b"hello"
    assert s3_store.exists("docs/foo.pdf")
    meta = s3_store.get_metadata("docs/foo.pdf")
    assert meta["content_type"] == "application/pdf"


def test_s3_exists_false_for_missing(s3_store) -> None:
    assert s3_store.exists("missing.pdf") is False
    with pytest.raises(KeyError):
        s3_store.get("missing.pdf")


def test_s3_delete_removes_object(s3_store) -> None:
    s3_store.put("to-delete.bin", b"x")
    s3_store.delete("to-delete.bin")
    assert not s3_store.exists("to-delete.bin")


def test_s3_presign_returns_signed_url(s3_store) -> None:
    s3_store.put("presign.pdf", b"x")
    url = s3_store.presign("presign.pdf", expires=60)
    assert url.startswith("https://") or url.startswith("http://")
    assert "test-bucket" in url or "X-Amz" in url


def test_s3_rejects_traversal_keys(s3_store) -> None:
    with pytest.raises(ValueError):
        s3_store.put("../escape.bin", b"x")


def test_minio_subclass_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """MinioObjectStore is a thin alias for S3ObjectStore; ensure it round-trips.

    moto's stand-alone-server mode is required for non-AWS endpoint URLs
    (it spins up a real local HTTP server that imitates S3 / MinIO).
    """
    pytest.importorskip("moto")
    from moto.server import ThreadedMotoServer

    from ai_employee.object_store.s3 import MinioObjectStore

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    try:
        host = f"http://127.0.0.1:{server._server.server_address[1]}"
        store = MinioObjectStore(
            bucket="test-bucket",
            access_key="testing",
            secret_key="testing",
            endpoint_url=host,
        )
        # MinIO doesn't auto-create buckets; create it via the store.
        import boto3

        boto3.client(
            "s3",
            endpoint_url=host,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="us-east-1",
        ).create_bucket(Bucket="test-bucket")
        store.put("a/b.pdf", b"minio", content_type="application/pdf")
        assert store.get("a/b.pdf") == b"minio"
        url = store.presign("a/b.pdf", expires=30)
        assert "127.0.0.1" in url or "X-Amz" in url
    finally:
        server.stop()