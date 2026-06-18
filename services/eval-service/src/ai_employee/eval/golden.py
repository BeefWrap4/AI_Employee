from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class GoldenLoadError(ValueError):
    """黄金集加载/校验错误。"""


@dataclass(frozen=True)
class GoldenItem:
    qid: str
    question: str
    expected_doc_title: str | None
    scope: list[str]
    expect_refusal: bool
    tags: list[str]


def _check_item(raw: dict, seen_qids: set[str]) -> GoldenItem:
    if not isinstance(raw, dict):
        raise GoldenLoadError("每行必须是 JSON object")
    qid = raw.get("qid")
    if not isinstance(qid, str) or not qid.strip():
        raise GoldenLoadError("qid 缺失或为空")
    if qid in seen_qids:
        raise GoldenLoadError(f"qid 重复: {qid!r}")
    seen_qids.add(qid)

    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        raise GoldenLoadError(f"{qid}: question 缺失或为空")

    title = raw.get("expected_doc_title")
    expect_refusal = bool(raw.get("expect_refusal", False))
    if expect_refusal:
        if title is not None:
            raise GoldenLoadError(
                f"{qid}: expect_refusal=true 时 expected_doc_title 必须为 null，得到 {title!r}"
            )
    else:
        if not (isinstance(title, str) and title.strip()):
            raise GoldenLoadError(
                f"{qid}: expect_refusal=false 时 expected_doc_title 必须非空字符串"
            )

    scope = raw.get("scope", [])
    if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
        raise GoldenLoadError(f"{qid}: scope 必须是字符串列表")

    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise GoldenLoadError(f"{qid}: tags 必须是字符串列表")

    return GoldenItem(
        qid=qid,
        question=question.strip(),
        expected_doc_title=title,
        scope=scope,
        expect_refusal=expect_refusal,
        tags=tags,
    )


def load_golden(path: str) -> list[GoldenItem]:
    p = Path(path)
    if not p.is_file():
        raise GoldenLoadError(f"找不到黄金集文件: {path}")
    text = p.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise GoldenLoadError(f"黄金集为空: {path}")

    items: list[GoldenItem] = []
    seen: set[str] = set()
    for idx, ln in enumerate(lines, start=1):
        try:
            raw = json.loads(ln)
        except json.JSONDecodeError as exc:
            raise GoldenLoadError(f"第 {idx} 行 JSON 解析失败: {exc}") from exc
        items.append(_check_item(raw, seen))
    return items


__all__ = ["GoldenItem", "GoldenLoadError", "load_golden"]
