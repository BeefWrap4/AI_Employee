"""Document versioning + chunk-level diff (spec §4.6).

Every published document snapshot is stored as an immutable
:class:`DocumentVersion` keyed by ``(doc_id, version)``.  Once written,
a version is never mutated — the only allowed write is to add a new
version (with a fresh ``vN`` tag).

:meth:`diff_versions` compares two versions at chunk granularity:

* ``added`` — chunk_ids present in the new version only.
* ``removed`` — chunk_ids present in the old version only.
* ``modified`` — chunk_ids present in both, but with different
  ``content`` (section_path changes do *not* count as modifications
  so re-classifying a heading doesn't pollute the diff).

In-process storage; swap in a SQL backend for production by
implementing the same public surface.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DocumentVersion:
    """Immutable snapshot of a document's chunks at one point in time."""

    doc_id: str
    version: str
    chunks: list[dict[str, Any]]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    notes: str | None = None

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "version": self.version,
            "chunk_count": self.chunk_count,
            "created_at": self.created_at,
            "notes": self.notes,
            "chunks": list(self.chunks),
        }


@dataclass
class DiffResult:
    """Output of :func:`diff_versions`."""

    from_version: str
    to_version: str
    added: list[str]
    removed: list[str]
    modified: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": list(self.modified),
            "summary": {
                "added": len(self.added),
                "removed": len(self.removed),
                "modified": len(self.modified),
            },
        }


def diff_versions(old: DocumentVersion, new: DocumentVersion) -> DiffResult:
    """Compute the chunk-level diff between two versions of one doc."""
    old_chunks = {c["chunk_id"]: c for c in old.chunks}
    new_chunks = {c["chunk_id"]: c for c in new.chunks}
    added = sorted(set(new_chunks) - set(old_chunks))
    removed = sorted(set(old_chunks) - set(new_chunks))
    # Modified: present in both, content differs.
    modified: list[str] = []
    for cid in sorted(set(old_chunks) & set(new_chunks)):
        if old_chunks[cid].get("content") != new_chunks[cid].get("content"):
            modified.append(cid)
    return DiffResult(
        from_version=old.version,
        to_version=new.version,
        added=added,
        removed=removed,
        modified=modified,
    )


class VersionStore:
    """Thread-safe in-process registry of immutable document versions."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, DocumentVersion]] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        doc_id: str,
        version: str,
        chunks: list[dict[str, Any]],
        notes: str | None = None,
    ) -> DocumentVersion:
        with self._lock:
            per_doc = self._versions.setdefault(doc_id, {})
            if version in per_doc:
                raise ValueError(
                    f"version {version!r} already exists for doc {doc_id!r}",
                )
            snapshot = DocumentVersion(
                doc_id=doc_id,
                version=version,
                chunks=list(chunks),  # defensive copy
                notes=notes,
            )
            per_doc[version] = snapshot
            return snapshot

    def get(self, doc_id: str, version: str) -> DocumentVersion | None:
        with self._lock:
            per_doc = self._versions.get(doc_id, {})
            return per_doc.get(version)

    def list_versions(self, doc_id: str) -> list[DocumentVersion]:
        with self._lock:
            per_doc = self._versions.get(doc_id, {})
            return [per_doc[v] for v in sorted(per_doc)]

    def latest(self, doc_id: str) -> DocumentVersion | None:
        versions = self.list_versions(doc_id)
        return versions[-1] if versions else None


_store = VersionStore()


def build_version_store() -> VersionStore:
    return _store


def new_version_tag(prefix: str = "v") -> str:
    """Generate a fresh ``v{random}`` version tag (avoids monotonic counters
    in single-process storage where two writers might race).
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


__all__ = [
    "DiffResult",
    "DocumentVersion",
    "VersionStore",
    "build_version_store",
    "diff_versions",
    "new_version_tag",
]