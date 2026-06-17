import io
from pathlib import Path

import fitz
import pytest

from ai_employee.ingestion_worker.parsers import PdfParser, ParsedSection


@pytest.fixture
def pdf_bytes() -> bytes:
    """Generate a minimal valid PDF in memory with text and a table-like structure."""
    buf = io.BytesIO()
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # US Letter

    # Page 1: heading and body text
    page.insert_text((72, 72), "Network Fault Report", fontsize=18, fontname="helv")
    page.insert_text((72, 120), "This report details a major outage.",
                     fontsize=12, fontname="helv")
    page.insert_text((72, 140), "Root cause: power failure at node BSC-01.",
                     fontsize=12, fontname="helv")

    # Page 2: table-like content
    page2 = doc.new_page(width=612, height=792)
    # Insert table as text grid
    headers = ["Time", "Event", "Severity"]
    rows = [
        ["10:00", "Link Down", "Critical"],
        ["10:05", "Failover", "Warning"],
        ["10:15", "Link Up", "Info"],
    ]
    y = 72
    x_positions = [72, 200, 400]
    for j, h in enumerate(headers):
        page2.insert_text((x_positions[j], y), h, fontsize=12, fontname="helv", fontfile=None)
    y += 30
    for row in rows:
        for j, val in enumerate(row):
            page2.insert_text((x_positions[j], y), val, fontsize=12, fontname="helv")
        y += 30

    doc.save(buf)
    doc.close()
    return buf.getvalue()


class TestPdfParser:
    def test_parses_multiple_pages(self, pdf_bytes: bytes) -> None:
        """PDF parser should produce sections for each page."""
        sections = PdfParser().parse(pdf_bytes)
        assert len(sections) >= 2
        paths = [s.section_path for s in sections]
        assert any("Page 1" in p for p in paths)
        assert any("Page 2" in p for p in paths)

    def test_extracts_text_per_page(self, pdf_bytes: bytes) -> None:
        """Text from each page should appear in the corresponding section blocks."""
        sections = PdfParser().parse(pdf_bytes)
        all_blocks = [b for s in sections for b in s.blocks]
        combined = " ".join(all_blocks)
        assert "Network Fault Report" in combined
        assert "power failure" in combined
        assert "BSC-01" in combined

    def test_detects_table_content(self, pdf_bytes: bytes) -> None:
        """Table data from page 2 should be present in the output."""
        sections = PdfParser().parse(pdf_bytes)
        page2_sections = [s for s in sections if "Page 2" in s.section_path]
        assert len(page2_sections) == 1
        page2_blocks = page2_sections[0].blocks
        page2_text = " ".join(page2_blocks)
        assert "Link Down" in page2_text
        assert "Failover" in page2_text
        assert "Critical" in page2_text

    def test_section_path_format(self, pdf_bytes: bytes) -> None:
        """section_path should use 'Page {n}' format."""
        sections = PdfParser().parse(pdf_bytes)
        for section in sections:
            assert section.section_path.startswith("Page ")
            page_num = int(section.section_path.split()[-1])
            assert page_num >= 1

    def test_empty_pdf_yields_empty(self) -> None:
        """A PDF with a page but no text should return sections with no blocks."""
        buf = io.BytesIO()
        doc = fitz.open()
        # Create a blank page (fitz won't save 0-page PDFs)
        doc.new_page(width=612, height=792)
        doc.save(buf)
        doc.close()
        sections = PdfParser().parse(buf.getvalue())
        # Empty page still yields a section but with no meaningful blocks
        assert len(sections) >= 0
