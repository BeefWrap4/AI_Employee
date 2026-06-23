"""R33-C2: structured table fields (columns + values) on ParsedChunk.

``ParsedChunk`` and ``ParsedSection`` gain optional ``columns`` /
``values`` fields (default ``None`` — backward compat).  ``XlsxParser``
populates them on its ``ParsedSection`` in addition to the existing
text-block representation, and ``chunk_sections`` propagates them onto
per-row ``ParsedChunk``s when a section has ``table_id`` + ``columns`` +
``values``.
"""

from __future__ import annotations

import io

import openpyxl
from ai_employee.common_schemas.knowledge import ParsedChunk
from ai_employee.ingestion_worker.chunker import chunk_sections
from ai_employee.ingestion_worker.parsers import ParsedSection, XlsxParser


def _xlsx_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Alarms"
    ws.append(["Time", "Event", "Severity"])
    ws.append(["10:00", "Link Down", "Critical"])
    ws.append(["10:05", "Failover", "Warning"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Schema: ParsedChunk has optional columns / values
# --------------------------------------------------------------------------- #


def test_parsed_chunk_columns_values_default_none() -> None:
    c = ParsedChunk(chunk_id="c1", chunk_no=1, content="x")
    assert c.columns is None
    assert c.values is None


def test_parsed_chunk_accepts_columns_values() -> None:
    c = ParsedChunk(
        chunk_id="c1",
        chunk_no=1,
        content="x",
        columns=["a", "b"],
        values=["1", "2"],
    )
    assert c.columns == ["a", "b"]
    assert c.values == ["1", "2"]


def test_parsed_chunk_columns_values_round_trip_json() -> None:
    import json

    c = ParsedChunk(
        chunk_id="c1",
        chunk_no=1,
        content="x",
        columns=["a", "b"],
        values=["1", "2"],
    )
    dumped = json.loads(c.model_dump_json())
    assert dumped["columns"] == ["a", "b"]
    assert dumped["values"] == ["1", "2"]


# --------------------------------------------------------------------------- #
# XlsxParser populates columns + values on its ParsedSection
# --------------------------------------------------------------------------- #


def test_xlsx_parser_section_has_columns() -> None:
    sections = XlsxParser().parse(_xlsx_bytes())
    assert len(sections) == 1
    section = sections[0]
    assert section.columns == ["Time", "Event", "Severity"]


def test_xlsx_parser_section_has_values() -> None:
    sections = XlsxParser().parse(_xlsx_bytes())
    section = sections[0]
    assert section.values is not None
    assert len(section.values) == 2  # two data rows
    assert section.values[0] == ["10:00", "Link Down", "Critical"]
    assert section.values[1] == ["10:05", "Failover", "Warning"]


def test_xlsx_parser_still_emits_text_blocks() -> None:
    """Backward compat: text blocks are still emitted alongside columns/values."""
    sections = XlsxParser().parse(_xlsx_bytes())
    section = sections[0]
    assert len(section.blocks) == 2
    assert "Link Down" in section.blocks[0]


# --------------------------------------------------------------------------- #
# chunk_sections propagates columns + per-row values to row chunks
# --------------------------------------------------------------------------- #


def test_chunked_row_carries_columns_and_values() -> None:
    section = ParsedSection(
        section_path="Alarms",
        blocks=["Time: 10:00 | Event: Link Down | Severity: Critical"],
        table_id="Alarms",
        row_ids=["row_001"],
        columns=["Time", "Event", "Severity"],
        values=[["10:00", "Link Down", "Critical"]],
    )
    chunks = chunk_sections("d1", [section])
    assert len(chunks) == 1
    c = chunks[0]
    assert c.table_id == "Alarms"
    assert c.row_id == "row_001"
    assert c.columns == ["Time", "Event", "Severity"]
    assert c.values == ["10:00", "Link Down", "Critical"]


def test_chunked_multiple_rows_each_carry_own_values() -> None:
    section = ParsedSection(
        section_path="Alarms",
        blocks=[
            "Time: 10:00 | Event: Link Down",
            "Time: 10:05 | Event: Failover",
        ],
        table_id="Alarms",
        row_ids=["row_001", "row_002"],
        columns=["Time", "Event"],
        values=[
            ["10:00", "Link Down"],
            ["10:05", "Failover"],
        ],
    )
    chunks = chunk_sections("d1", [section])
    assert len(chunks) == 2
    by_row = {c.row_id: c for c in chunks}
    assert by_row["row_001"].values == ["10:00", "Link Down"]
    assert by_row["row_002"].values == ["10:05", "Failover"]
    # columns are the same for every row chunk.
    assert all(c.columns == ["Time", "Event"] for c in chunks)


def test_chunked_section_without_columns_leaves_chunk_fields_none() -> None:
    """A prose section (no columns/values) still yields columns=None / values=None."""
    section = ParsedSection(section_path="intro", blocks=["some prose"])
    chunks = chunk_sections("d1", [section])
    assert chunks
    assert all(c.columns is None for c in chunks)
    assert all(c.values is None for c in chunks)


def test_end_to_end_xlsx_to_chunk_carries_columns_and_values() -> None:
    sections = XlsxParser().parse(_xlsx_bytes())
    chunks = chunk_sections("d1", sections)
    assert len(chunks) == 2
    by_row = {c.row_id: c for c in chunks}
    assert by_row["row_001"].columns == ["Time", "Event", "Severity"]
    assert by_row["row_001"].values == ["10:00", "Link Down", "Critical"]
    assert by_row["row_002"].values == ["10:05", "Failover", "Warning"]
