"""Table-structure field propagation tests (R17-3 / spec §5.2).

``ParsedSection`` already carries ``table_id`` + ``row_ids`` for
structured (xlsx/csv) sources, but ``chunk_sections`` dropped them —
every chunk lost its table provenance.  This propagates the fields to
``ParsedChunk`` (and through to ``ChunkRecord``) so retrieval can filter
and cite by table + row.
"""
from __future__ import annotations

from ai_employee.common_schemas.knowledge import ChunkRecord, ParsedChunk
from ai_employee.ingestion_worker.chunker import chunk_sections
from ai_employee.ingestion_worker.parsers import ParsedSection


def _table_section(
    table_id: str = "Sheet1",
    rows: list[tuple[str, str]] | None = None,
) -> ParsedSection:
    rows = rows or [("row_001", "col1: a | col2: b"), ("row_002", "col1: c | col2: d")]
    return ParsedSection(
        section_path=table_id,
        blocks=[r[1] for r in rows],
        table_id=table_id,
        row_ids=[r[0] for r in rows],
    )


# --------------------------------------------------------------------------- #
# Schema: ParsedChunk carries table_id / row_id
# --------------------------------------------------------------------------- #


def test_parsed_chunk_has_optional_table_fields() -> None:
    c = ParsedChunk(chunk_id="c1", chunk_no=1, content="x")
    assert c.table_id is None
    assert c.row_id is None


def test_parsed_chunk_accepts_table_fields() -> None:
    c = ParsedChunk(
        chunk_id="c1", chunk_no=1, content="x",
        table_id="Sheet1", row_id="row_003",
    )
    assert c.table_id == "Sheet1"
    assert c.row_id == "row_003"


def test_chunk_record_has_table_fields() -> None:
    r = ChunkRecord(
        chunk_id="c1", doc_id="d1", chunk_no=1, content="x",
        table_id="Sheet1", row_id="row_003",
    )
    assert r.table_id == "Sheet1"
    assert r.row_id == "row_003"


def test_chunk_record_table_fields_default_none() -> None:
    r = ChunkRecord(chunk_id="c1", doc_id="d1", chunk_no=1, content="x")
    assert r.table_id is None
    assert r.row_id is None


# --------------------------------------------------------------------------- #
# chunk_sections propagation
# --------------------------------------------------------------------------- #


def test_table_section_propagates_table_id_to_chunks() -> None:
    section = _table_section("Inventory")
    chunks = chunk_sections("d1", [section])
    assert chunks
    assert all(c.table_id == "Inventory" for c in chunks)


def test_table_section_propagates_row_id_per_block() -> None:
    """Each block (row) becomes a chunk carrying its own row_id."""
    section = _table_section("Inventory", [
        ("row_001", "col1: a | col2: b"),
        ("row_002", "col1: c | col2: d"),
    ])
    chunks = chunk_sections("d1", [section])
    row_ids = {c.row_id for c in chunks}
    assert "row_001" in row_ids
    assert "row_002" in row_ids


def test_table_section_row_ids_align_with_blocks() -> None:
    """The i-th chunk's row_id matches the i-th block's row_id."""
    section = _table_section("Inventory", [
        ("row_001", "alpha"),
        ("row_002", "beta"),
    ])
    chunks = chunk_sections("d1", [section])
    # Each block is short → one chunk per row, in order.
    assert [c.row_id for c in chunks] == ["row_001", "row_002"]


def test_prose_section_has_no_table_fields() -> None:
    """Plain prose sections produce table_id=None / row_id=None chunks."""
    section = ParsedSection(section_path="intro", blocks=["some prose"])
    chunks = chunk_sections("d1", [section])
    assert all(c.table_id is None for c in chunks)
    assert all(c.row_id is None for c in chunks)


def test_mixed_table_and_prose_sections() -> None:
    sections = [
        ParsedSection(section_path="intro", blocks=["prose here"]),
        _table_section("Sheet1", [("row_001", "data row")]),
    ]
    chunks = chunk_sections("d1", sections)
    table_chunks = [c for c in chunks if c.table_id == "Sheet1"]
    prose_chunks = [c for c in chunks if c.table_id is None]
    assert table_chunks
    assert prose_chunks
    assert all(c.row_id == "row_001" for c in table_chunks)
    assert all(c.row_id is None for c in prose_chunks)


def test_table_section_without_row_ids_propagates_table_id_only() -> None:
    """A table section with table_id but no row_ids still sets table_id."""
    section = ParsedSection(
        section_path="Sheet1", blocks=["merged header"], table_id="Sheet1",
    )
    chunks = chunk_sections("d1", [section])
    assert all(c.table_id == "Sheet1" for c in chunks)
    assert all(c.row_id is None for c in chunks)


def test_long_table_row_split_preserves_table_id() -> None:
    """A very long table row that splits into multiple chunks keeps table_id."""
    long_row = "col: " + "x" * 2000
    section = _table_section("Big", [("row_001", long_row)])
    chunks = chunk_sections("d1", [section])
    assert len(chunks) >= 2
    # All pieces of the same row carry the same table_id + row_id.
    assert all(c.table_id == "Big" for c in chunks)
    assert all(c.row_id == "row_001" for c in chunks)


def test_table_id_round_trips_through_json() -> None:
    import json

    c = ParsedChunk(
        chunk_id="c1", chunk_no=1, content="x",
        table_id="Sheet1", row_id="row_005",
    )
    dumped = json.loads(c.model_dump_json())
    assert dumped["table_id"] == "Sheet1"
    assert dumped["row_id"] == "row_005"
    reloaded = ParsedChunk(**dumped)
    assert reloaded.table_id == "Sheet1"
    assert reloaded.row_id == "row_005"
