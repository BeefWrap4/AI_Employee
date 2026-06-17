"""ACL 解析：多 scope 表达式 + doc_id 解析。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_employee.knowledge_api.store import SQLiteStore


def resolve_visible_docs(
    store: "SQLiteStore",
    scope: list[str] | None,
    scope_or: list[str] | None,
) -> list[str]:
    """根据 scope + scope_or 解析可见的已发布 doc_id 列表。

    规则：
      - 文档需 parse_status='published'
      - documents.acl_tags ∪ metadata.values 与 (set(scope) ∪ set(scope_or)) 有交集
      - scope 与 scope_or 都为空 → 返回所有 published doc
      - 返回按 doc_id 升序
    """
    effective = set(scope or []) | set(scope_or or [])
    items, total = store.list_documents(status="published", page=1, page_size=200)
    if not effective:
        return sorted(d["doc_id"] for d in items)
    result: list[str] = []
    for d in items:
        visible = set(d.get("acl_tags", [])) | {
            str(v) for v in d.get("metadata", {}).values()
        }
        if visible & effective:
            result.append(d["doc_id"])
    return sorted(result)


__all__ = ["resolve_visible_docs"]
