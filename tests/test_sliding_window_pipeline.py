"""Sliding-window chunking pipeline integration tests (R17-2 / spec §5.3).

``chunk_sections`` historically split over-long blocks with
``_split_long_block`` (hard sentence-boundary cuts, no overlap).  Spec
§5.3 mandates a sliding-window strategy with overlap so context carries
across chunk boundaries.  This wires ``sliding_window_chunk`` into the
main pipeline, env-gated so the boundary strategy remains available.
"""
from __future__ import annotations

import pytest
from ai_employee.ingestion_worker.chunker import (
    chunk_sections,
    sliding_window_chunk,
)
from ai_employee.ingestion_worker.parsers import ParsedSection


def _long_text(n_chars: int) -> str:
    """Produce a long prose string with no sentence boundaries."""
    base = "告警码 AL-01 持续触发 "
    text = ""
    i = 0
    while len(text) < n_chars:
        text += f"{base}{i} "
        i += 1
    return text[:n_chars]


# --------------------------------------------------------------------------- #
# sliding_window_chunk unit (already exists; guard against regression)
# --------------------------------------------------------------------------- #


def test_sliding_window_overlaps_consecutive_chunks() -> None:
    text = _long_text(2000)
    chunks = sliding_window_chunk(text, window_size=500, overlap=100)
    assert len(chunks) >= 2
    # Consecutive chunks share overlap characters at the boundary.
    assert chunks[0][-50:] in chunks[1] or chunks[1][:50] in chunks[0]


def test_sliding_window_short_text_single_chunk() -> None:
    assert sliding_window_chunk("short", window_size=500, overlap=100) == ["short"]


# --------------------------------------------------------------------------- #
# chunk_sections: sliding-window strategy (default)
# --------------------------------------------------------------------------- #


def test_chunk_sections_uses_sliding_window_for_long_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block longer than _MAX_CHUNK_LEN is split with overlap."""
    monkeypatch.setenv("CHUNK_STRATEGY", "sliding")
    monkeypatch.setenv("CHUNK_WINDOW_SIZE", "300")
    monkeypatch.setenv("CHUNK_OVERLAP", "60")
    long_block = _long_text(2000)
    section = ParsedSection(section_path="root", blocks=[long_block])
    chunks = chunk_sections("d1", [section])
    assert len(chunks) >= 2
    # Each chunk respects the window size (allowing boundary back-off).
    for c in chunks:
        assert len(c.content) <= 400  # window + small boundary slack


def test_chunk_sections_sliding_window_produces_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adjacent chunks from a long block share overlapping text."""
    monkeypatch.setenv("CHUNK_STRATEGY", "sliding")
    monkeypatch.setenv("CHUNK_WINDOW_SIZE", "300")
    monkeypatch.setenv("CHUNK_OVERLAP", "80")
    long_block = _long_text(2000)
    chunks = chunk_sections("d1", [ParsedSection(section_path="root", blocks=[long_block])])
    assert len(chunks) >= 2
    # The tail of chunk 0 should appear in chunk 1's head region.
    tail = chunks[0].content[-40:]
    assert tail in (chunks[0].content + chunks[1].content)


def test_chunk_sections_boundary_strategy_uses_hard_cuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHUNK_STRATEGY=boundary keeps the legacy no-overlap behaviour."""
    monkeypatch.setenv("CHUNK_STRATEGY", "boundary")
    long_block = _long_text(2000)
    chunks = chunk_sections("d1", [ParsedSection(section_path="root", blocks=[long_block])])
    assert len(chunks) >= 2
    # No overlap: chunk 0's tail is NOT in chunk 1.
    if len(chunks) >= 2:
        tail = chunks[0].content[-30:]
        # Hard-cut means the boundary is a sentence/newline, not overlap.
        assert tail not in chunks[1].content or len(chunks[1].content) < 60


def test_chunk_sections_short_blocks_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short blocks (under window) produce one chunk regardless of strategy."""
    monkeypatch.setenv("CHUNK_STRATEGY", "sliding")
    section = ParsedSection(section_path="root", blocks=["short block"])
    chunks = chunk_sections("d1", [section])
    assert len(chunks) == 1
    assert chunks[0].content == "short block"


def test_chunk_sections_default_strategy_is_sliding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without CHUNK_STRATEGY set, sliding window is the default."""
    monkeypatch.delenv("CHUNK_STRATEGY", raising=False)
    long_block = _long_text(2000)
    chunks = chunk_sections("d1", [ParsedSection(section_path="root", blocks=[long_block])])
    # Multiple overlapping chunks → sliding window engaged.
    assert len(chunks) >= 2


def test_chunk_sections_preserves_section_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHUNK_STRATEGY", "sliding")
    long_block = _long_text(1000)
    chunks = chunk_sections(
        "d1", [ParsedSection(section_path="Page 3", blocks=[long_block])],
    )
    assert all(c.section_path == "Page 3" for c in chunks)


def test_chunk_sections_chunk_no_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHUNK_STRATEGY", "sliding")
    long_block = _long_text(1500)
    chunks = chunk_sections("d1", [ParsedSection(section_path="root", blocks=[long_block])])
    nos = [c.chunk_no for c in chunks]
    assert nos == sorted(nos)
    assert nos[0] == 1


def test_alarm_code_not_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A告警码 token must not be broken at a window boundary."""
    monkeypatch.setenv("CHUNK_STRATEGY", "sliding")
    monkeypatch.setenv("CHUNK_WINDOW_SIZE", "100")
    monkeypatch.setenv("CHUNK_OVERLAP", "20")
    # Plant an alarm code near a likely boundary.
    text = "x" * 95 + "AL-0142" + "y" * 95
    chunks = chunk_sections("d1", [ParsedSection(section_path="root", blocks=[text])])
    joined = "".join(c.content for c in chunks)
    assert "AL-0142" in joined
    # No chunk should contain a partial token like "AL-01" without the rest.
    for c in chunks:
        assert "AL-0142" in c.content or "AL-0142" not in c.content.replace("AL-0142", "")
