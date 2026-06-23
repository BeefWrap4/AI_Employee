from __future__ import annotations

import os
import re

from ai_employee.common_schemas.knowledge import ParsedChunk

_MAX_CHUNK_LEN = 800
_MERGE_MAX_LEN = 15
_MERGE_MAX_BLOCKS = 3
_ALARM_CODE_RE = re.compile(r"[A-Z]{2,}-\d{2,}")
_BOUNDARY_CHARS = ("。", "！", "?", "\n", "；")

# Sliding-window strategy config (R17-2 / spec §5.3).  Defaults to the
# sliding strategy with a window matching the legacy _MAX_CHUNK_LEN and
# a 10% overlap so context carries across boundaries.
_DEFAULT_WINDOW = 800
_DEFAULT_OVERLAP = 80


def _strategy() -> str:
    return os.getenv("CHUNK_STRATEGY", "sliding").strip().lower()


def _window_size() -> int:
    try:
        return max(64, int(os.getenv("CHUNK_WINDOW_SIZE", str(_DEFAULT_WINDOW))))
    except ValueError:
        return _DEFAULT_WINDOW


def _overlap() -> int:
    try:
        return max(0, int(os.getenv("CHUNK_OVERLAP", str(_DEFAULT_OVERLAP))))
    except ValueError:
        return _DEFAULT_OVERLAP


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


def sliding_window_chunk(
    text: str,
    *,
    window_size: int,
    overlap: int,
) -> list[str]:
    """Split a long text into overlapping chunks.

    Each chunk is at most ``window_size`` characters; consecutive chunks
    share ``overlap`` characters so context carries across boundaries.
    The advance per step is ``window_size - overlap``.

    When ``overlap >= window_size`` the function clamps the advance to
    1 to guarantee forward progress.  Empty / short text returns a
    single chunk (or empty list for the empty string).
    """
    text = text or ""
    if not text.strip():
        return []
    if window_size <= 0:
        raise ValueError("window_size must be > 0")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if len(text) <= window_size:
        return [text]

    advance = max(1, window_size - overlap)
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + window_size, n)
        # Try to back off to a sentence boundary when we are mid-text.
        if end < n:
            best = -1
            for ch in _BOUNDARY_CHARS:
                idx = text.rfind(ch, start + 1, end)
                if idx > best:
                    best = idx
            if best > start + window_size // 2:
                end = best + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start += advance
    return chunks


def chunk_sections(doc_id: str, sections: list) -> list[ParsedChunk]:
    """把 ParsedSection 列表切成 ParsedChunk 列表。

    策略：段落级 → 窗口合并 → 超长截断 → 告警码保护。

    超长块切分按 ``CHUNK_STRATEGY`` 选择（R17-2 / spec §5.3）：

    * ``sliding``（默认）—— 滑动窗口重叠分段，``CHUNK_WINDOW_SIZE`` /
      ``CHUNK_OVERLAP`` 控制窗口与重叠量，相邻 chunk 共享重叠文本，告警码
      保护仍在（``sliding_window_chunk`` 在窗口边界回退到句号/换行）。
    * ``boundary`` —— 旧行为，``_split_long_block`` 按句号/换行硬切无重叠。
    """
    strategy = _strategy()
    window = _window_size()
    overlap = _overlap()
    chunks: list[ParsedChunk] = []
    seq = 0

    def _split_merged(merged: str) -> list[str]:
        if strategy == "sliding":
            pieces = sliding_window_chunk(merged, window_size=window, overlap=overlap)
            # sliding_window_chunk already respects window size; only apply
            # the hard-cut alarm-code guard as a safety net on any piece
            # that still exceeds the window (boundary back-off can overshoot).
            out: list[str] = []
            for p in pieces:
                if len(p) <= window:
                    out.append(p)
                else:
                    out.extend(_split_long_block(p, window))
            return out
        return _split_long_block(merged, _MAX_CHUNK_LEN)

    for section in sections:
        table_id = section.table_id
        row_ids = section.row_ids or []
        # Structured table fields (R33-C2): when a table section carries
        # columns + values, propagate them to per-row chunks.  ``values[i]``
        # is parallel to ``blocks`` / ``row_ids``.
        section_columns = section.columns
        section_values = section.values or []
        # Merge buffer now tracks (block_text, row_id, row_values) tuples so
        # table provenance + structured values carry through to each chunk.
        buffer: list[tuple[str, str | None, list[str] | None]] = []
        buffer_len = 0

        def flush(path: str, tbl_id: str | None, columns: list[str] | None) -> None:
            nonlocal seq, buffer, buffer_len
            if not buffer:
                return
            merged = " ".join(b for b, _, _ in buffer)
            # When the buffer holds a single table row, propagate its row_id
            # and (if present) that row's structured values; merged rows lose
            # per-row identity (table_id still carries).
            row_id: str | None = None
            row_values: list[str] | None = None
            if len(buffer) == 1:
                row_id = buffer[0][1]
                row_values = buffer[0][2]
            # columns propagate for any table chunk (same for every row).
            chunk_columns = columns if tbl_id is not None else None
            # Only attach per-row values when this is a single-row chunk
            # (merged rows cannot be attributed to one value list).
            chunk_values = row_values if (tbl_id is not None and len(buffer) == 1) else None
            for piece in _split_merged(merged):
                seq += 1
                chunks.append(
                    ParsedChunk(
                        chunk_id=f"chunk_{doc_id}_{seq:03d}",
                        chunk_no=seq,
                        content=piece,
                        section_path=path,
                        table_id=tbl_id,
                        row_id=row_id,
                        columns=chunk_columns,
                        values=chunk_values,
                    )
                )
            buffer = []
            buffer_len = 0

        for idx, block in enumerate(section.blocks):
            block = block.strip()
            if not block:
                continue
            row_id = row_ids[idx] if idx < len(row_ids) else None
            row_vals = section_values[idx] if idx < len(section_values) else None
            prospective_len = buffer_len + len(block) + 1
            # Table rows keep their per-row identity (one chunk per row) so
            # row_id stays meaningful; only prose blocks merge together.
            can_merge = (
                table_id is None
                and buffer
                and prospective_len <= _MERGE_MAX_LEN
                and len(buffer) < _MERGE_MAX_BLOCKS
            )
            if can_merge:
                buffer.append((block, row_id, row_vals))
                buffer_len = prospective_len
            else:
                flush(section.section_path, table_id, section_columns)
                buffer = [(block, row_id, row_vals)]
                buffer_len = len(block)
        flush(section.section_path, table_id, section_columns)

    return chunks
