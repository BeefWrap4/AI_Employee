from __future__ import annotations

from fastapi import Header, HTTPException, status


def require_internal_token(expected_token: str):
    """返回一个依赖函数，校验 X-Internal-Token 头。"""

    def _dep(x_internal_token: str | None = Header(default=None)) -> None:
        if not expected_token or x_internal_token != expected_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "internal_unauthorized"},
            )

    return _dep
