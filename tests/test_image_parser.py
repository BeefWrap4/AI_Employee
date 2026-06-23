"""R33-C1: image MIME parser registration + OCR delegation.

``ImageParser`` delegates to an OCR backend (default
``build_ocr_backend()``) and returns a single ``ParsedSection`` whose
``section_path`` is ``"ocr"``.  When OCR is disabled (the default env),
the parser must still return a section with placeholder text rather than
crash, so image ingestion degrades gracefully instead of 500-ing.
"""

from __future__ import annotations

import pytest
from ai_employee.ingestion_worker.ocr import DisabledOcrBackend, FakeOcrBackend
from ai_employee.ingestion_worker.parsers import ImageParser, get_parser


def test_get_parser_routes_image_png() -> None:
    parser = get_parser("image/png")
    assert isinstance(parser, ImageParser)


def test_get_parser_routes_image_jpeg() -> None:
    parser = get_parser("image/jpeg")
    assert isinstance(parser, ImageParser)


def test_image_parser_yields_ocr_text_with_stubbed_backend() -> None:
    fake = FakeOcrBackend()
    fake.seed_text("告警 AL-01\n站点 NodeB-42")
    parser = ImageParser(ocr_backend=fake)
    sections = parser.parse(b"\x89PNG fake image bytes")
    assert len(sections) == 1
    section = sections[0]
    assert section.section_path == "ocr"
    joined = " ".join(section.blocks)
    assert "AL-01" in joined
    assert "NodeB-42" in joined


def test_image_parser_disabled_backend_returns_placeholder_section() -> None:
    """OCR_BACKEND=disabled (default) → no crash, returns a placeholder section."""
    parser = ImageParser(ocr_backend=DisabledOcrBackend())
    sections = parser.parse(b"\x89PNG fake image bytes")
    # Must return exactly one section (not raise); content may be placeholder.
    assert len(sections) == 1
    assert sections[0].section_path == "ocr"
    # Placeholder section carries an explanatory block (possibly empty-ish)
    # but crucially does NOT crash.
    assert isinstance(sections[0].blocks, list)


def test_image_parser_accepts_str_path_and_bytes(tmp_path) -> None:
    fake = FakeOcrBackend()
    fake.seed_text("scanned text AL-77")
    parser = ImageParser(ocr_backend=fake)
    # bytes path
    sections = parser.parse(b"fake bytes")
    assert any("AL-77" in b for s in sections for b in s.blocks)
    # str path
    p = tmp_path / "scan.png"
    p.write_bytes(b"fake")
    sections2 = parser.parse(str(p))
    assert any("AL-77" in b for s in sections2 for b in s.blocks)


def test_image_parser_default_backend_is_built_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no backend is injected, ImageParser builds one from OCR_BACKEND."""
    monkeypatch.setenv("OCR_BACKEND", "disabled")
    parser = ImageParser()
    # Must not crash on parse and must yield a placeholder section.
    sections = parser.parse(b"fake")
    assert len(sections) == 1
    assert sections[0].section_path == "ocr"
