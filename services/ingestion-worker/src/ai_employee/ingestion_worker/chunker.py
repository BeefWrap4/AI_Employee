from __future__ import annotations

import re

from ai_employee.common_schemas.knowledge import ParsedChunk

_MAX_CHUNK_LEN = 800
_MERGE_MAX_LEN = 15
_MERGE_MAX_BLOCKS = 3
_ALARM_CODE_RE = re.compile(r"[A-Z]{2,}-\d{2,}")


def _split_long_block(text: str, max_len: int) -> list[str]:
    """超长块按句号/换行硬切，保护告警码不被拆开。"""
    if len(text) <= max_len:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        if end < len(text):
            cut = max(text.rfind("。", start, end), text.rfind("\n", start, end))
            if cut > start:
                end = cut + 1
            else:
                # 没有合适分隔符；检查 end 位置是否落在告警码中间
                m = _ALARM_CODE_RE.search(text, start, end + 8)
                if m and m.start() < end < m.end():
                    end = m.start()
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    return pieces


def chunk_sections(doc_id: str, sections: list) -> list[ParsedChunk]:
    """把 ParsedSection 列表切成 ParsedChunk 列表。

    策略：段落级 → 窗口合并 → 超长截断 → 告警码保护。
    """
    chunks: list[ParsedChunk] = []
    seq = 0

    for section in sections:
        buffer: list[str] = []
        buffer_len = 0

        def flush(path: str) -> None:
            nonlocal seq, buffer, buffer_len
            if not buffer:
                return
            merged = " ".join(buffer)
            for piece in _split_long_block(merged, _MAX_CHUNK_LEN):
                seq += 1
                chunks.append(
                    ParsedChunk(
                        chunk_id=f"chunk_{doc_id}_{seq:03d}",
                        chunk_no=seq,
                        content=piece,
                        section_path=path,
                    )
                )
            buffer = []
            buffer_len = 0

        for block in section.blocks:
            block = block.strip()
            if not block:
                continue
            prospective_len = buffer_len + len(block) + 1
            if buffer and prospective_len <= _MERGE_MAX_LEN and len(buffer) < _MERGE_MAX_BLOCKS:
                buffer.append(block)
                buffer_len = prospective_len
            else:
                flush(section.section_path)
                buffer = [block]
                buffer_len = len(block)
        flush(section.section_path)

    return chunks
