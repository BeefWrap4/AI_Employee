"""Adapters between HTTP request shapes and internal supplement storage.

The supplement flow (R20-1) used to store attachments as
``{"name": ..., "uri": ..., "content_type": ...}`` and persisted them
verbatim on the task.  R22 lets callers also reference an uploaded
object by its ``object_key`` (returned from
``POST /api/v1/objects``).  This module translates that wire shape
into a uniform dict shape the runtime layer still understands:

    {"name": ..., "uri": ..., "object_key": ..., "content_type": ...,
     "size": ..., "metadata": ...}

For ``object_key``-only attachments we also resolve a download URL via
the configured :class:`ObjectStore` so the reviewer can open it
without us needing to re-upload the bytes.  Reviewers using the
LocalFs backend get the same-origin ``/api/v1/objects/{key}/download``
URL; S3 / MinIO callers get a presigned S3 URL.
"""

from __future__ import annotations

from typing import Any

from ai_employee.object_store import build_object_store


def normalize_attachment(
    att: dict[str, Any] | Any,
    *,
    allow_object_lookup: bool = True,
) -> dict[str, Any]:
    """Coerce a SupplementAttachment-like dict into the runtime shape.

    Rules:

    * If ``object_key`` is set and ``uri`` is empty, derive ``uri``
      from the object store's presign URL.
    * Reject the request if both ``object_key`` and ``uri`` are missing.
    * Reject ``object_key`` paths that fail key validation (caught at
      the object-store layer when ``put`` / ``presign`` runs).
    """
    if hasattr(att, "model_dump"):
        data = att.model_dump()
    elif isinstance(att, dict):
        data = dict(att)
    else:  # pragma: no cover - defensive
        raise TypeError(f"unsupported attachment type: {type(att)!r}")

    object_key = data.get("object_key")
    uri = data.get("uri")

    if object_key and not uri and allow_object_lookup:
        # Resolve to a download URL through the configured store.
        store = build_object_store()
        try:
            uri = store.presign(object_key, expires=3600)
        except KeyError:
            # The object hasn't been uploaded yet — leave uri unset so the
            # reviewer can see the gap; the runtime layer still stores the
            # reference.
            uri = None

    if not uri and not object_key:
        raise ValueError("attachment must include either uri or object_key")

    data["uri"] = uri
    return data


def normalize_attachments(items: list[Any]) -> list[dict[str, Any]]:
    """Apply :func:`normalize_attachment` to a list."""
    return [normalize_attachment(a) for a in items]


__all__ = ["normalize_attachment", "normalize_attachments"]
