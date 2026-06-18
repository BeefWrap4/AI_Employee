"""common_schemas 异常定义。"""

from __future__ import annotations


class IndexCorruptedError(RuntimeError):
    """FTS5 索引损坏，启动期探活失败时抛出。"""


__all__ = ["IndexCorruptedError"]
