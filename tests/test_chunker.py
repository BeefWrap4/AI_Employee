from ai_employee.ingestion_worker.chunker import chunk_sections
from ai_employee.ingestion_worker.parsers import ParsedSection


def test_each_block_becomes_chunk_when_large_enough() -> None:
    sections = [
        ParsedSection(
            section_path="root",
            blocks=["这是一段足够长的正文内容，长度超过窗口合并的阈值以便独立成块。"],
        )
    ]
    chunks = chunk_sections("doc_001", sections)
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "chunk_doc_001_001"
    assert chunks[0].chunk_no == 1
    assert chunks[0].section_path == "root"


def test_small_adjacent_blocks_are_merged() -> None:
    sections = [
        ParsedSection(
            section_path="root",
            blocks=["短段一。", "短段二。", "短段三。"],
        )
    ]
    chunks = chunk_sections("doc_001", sections)
    assert len(chunks) == 1
    assert "短段一" in chunks[0].content
    assert "短段三" in chunks[0].content


def test_overlong_block_is_truncated() -> None:
    long_block = "正文内容。" * 200  # 远超 800 字
    sections = [ParsedSection(section_path="root", blocks=[long_block])]
    chunks = chunk_sections("doc_001", sections)
    assert len(chunks) >= 1
    for chunk in chunks:
        assert len(chunk.content) <= 800


def test_alarm_code_not_split() -> None:
    block = "前文内容足够长以触发切分。" * 30 + "告警码 AL-12 表示故障。"
    sections = [ParsedSection(section_path="root", blocks=[block])]
    chunks = chunk_sections("doc_001", sections)
    joined = "".join(c.content for c in chunks)
    assert "AL-12" in joined


def test_chunk_no_is_sequential_across_sections() -> None:
    sections = [
        ParsedSection(section_path="A", blocks=["段落甲足够长以独立成块。"]),
        ParsedSection(section_path="B", blocks=["段落乙足够长以独立成块。"]),
    ]
    chunks = chunk_sections("doc_001", sections)
    assert [c.chunk_no for c in chunks] == [1, 2]
    assert chunks[0].section_path == "A"
    assert chunks[1].section_path == "B"
    assert chunks[1].chunk_id == "chunk_doc_001_002"


def test_empty_sections_produce_no_chunks() -> None:
    assert chunk_sections("doc_001", []) == []
