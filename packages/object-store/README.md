# Object Store Abstraction (R22)

A thin protocol for binary object storage that lets the same code run
against the local filesystem (dev / single-node) or against an
S3-compatible object store (AWS S3, MinIO, etc.).

## Why

Large binary objects (uploaded PDFs / images, supplement attachments,
parsed document backups) used to live on the local volume under
``./var/uploads/`` / ``./var/data/raw/``.  That ties deployments to a
single node and complicates horizontal scaling.  R22 introduces an
``ObjectStore`` abstraction so we can move those blobs into S3 / MinIO
without rewriting callers.

## Backends

| Class                | When                                       | Requirements        |
|----------------------|--------------------------------------------|---------------------|
| ``LocalFsObjectStore`` | Default (dev / test). No external service. | None.               |
| ``S3ObjectStore``    | Production. Also used for MinIO.           | ``boto3``.          |

The factory ``build_object_store()`` selects based on env vars:

```python
OBJECT_STORE_URL=http://minio:9000        # → S3 / MinIO
OBJECT_STORE_ACCESS_KEY=minioadmin
OBJECT_STORE_SECRET_KEY=minioadmin
OBJECT_STORE_BUCKET=ai-employee

# Unset OBJECT_STORE_URL → LocalFs
OBJECT_STORE_LOCAL_ROOT=./var/objects
```

## API

```python
store = build_object_store()
store.put("docs/foo.pdf", b"...", content_type="application/pdf")
store.exists("docs/foo.pdf")            # True
store.get_metadata("docs/foo.pdf")      # {"content_type": ..., "size": ...}
store.presign("docs/foo.pdf", expires=60)
data = store.get("docs/foo.pdf")
store.delete("docs/foo.pdf")
```

Keys must be relative POSIX paths; ``..`` segments and absolute keys
are rejected to prevent traversal.

## Backward compatibility

Services that previously wrote to local paths (e.g.
``./var/data/raw/{doc_id}.pdf``) now write through the object store
*and* keep a local copy when ``OBJECT_STORE_URL`` is unset (LocalFs
writes to the configured ``OBJECT_STORE_LOCAL_ROOT``, default
``./var/objects``, leaving the original on-disk layout untouched in
the absence of explicit migration).  When ``OBJECT_STORE_URL`` *is*
set, callers should fall back to a read from the LocalFs root for
already-uploaded objects so existing ingestion tests don't break.