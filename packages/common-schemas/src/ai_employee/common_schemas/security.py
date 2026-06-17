"""source_uri 路径安全校验。"""
from __future__ import annotations

from pathlib import Path


class UnsafeSourceUriError(ValueError):
    """source_uri 不符合安全约束。"""


def assert_safe_source_uri(path: str, data_dir: str) -> str:
    """校验 path 位于 data_dir/raw/ 之下且为绝对路径，返回规范化路径。

    规则：
      1. data_dir 必须可 resolve 为绝对路径；
      2. path 必须为绝对路径；
      3. Path(path).resolve(strict=False) 必须位于 Path(data_dir).resolve()/"raw" 之下。
    """
    if not data_dir:
        raise UnsafeSourceUriError("invalid data_dir: empty")

    try:
        raw_root = (Path(data_dir) / "raw").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafeSourceUriError(f"invalid data_dir: {data_dir!r} ({exc})") from exc

    if not Path(path).is_absolute():
        raise UnsafeSourceUriError(f"source_uri not absolute: {path!r}")

    try:
        resolved = Path(path).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafeSourceUriError(f"cannot resolve source_uri: {path!r} ({exc})") from exc

    try:
        resolved.relative_to(raw_root)
    except ValueError as exc:
        raise UnsafeSourceUriError(
            f"source_uri outside data_dir/raw: {resolved} not under {raw_root}"
        ) from exc

    return str(resolved)


__all__ = ["UnsafeSourceUriError", "assert_safe_source_uri"]
