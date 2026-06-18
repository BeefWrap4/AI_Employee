"""OCR parser tests (R17-1 / spec §5.2 扫描件 OCR).

The :class:`OcrParser` extracts text from scanned PDFs / images via a
pluggable OCR backend.  Backends:

* :class:`FakeOcrBackend` — deterministic, for tests.
* :class:`RapidOcrBackend` — wraps the ``rapidocr_onnxruntime`` package
  (preferred; CPU-friendly, no system binary).
* :class:`TesseractOcrBackend` — shells out to ``tesseract`` (fallback).

When no backend is available (``OCR_BACKEND=disabled`` or deps missing),
the parser degrades gracefully: returns an empty section list + a
warning flag so ingestion can mark the doc parse_failed rather than crash.
"""
from __future__ import annotations

import pytest
from ai_employee.ingestion_worker.ocr import (
    FakeOcrBackend,
    OcrParser,
    OcrResult,
    RapidOcrBackend,
    TesseractOcrBackend,
    build_ocr_backend,
)

# --------------------------------------------------------------------------- #
# OcrResult
# --------------------------------------------------------------------------- #


def test_ocr_result_carries_text_and_pages() -> None:
    r = OcrResult(text="告警码 AL-01", pages=2, backend="fake")
    assert r.text == "告警码 AL-01"
    assert r.pages == 2
    assert r.backend == "fake"


def test_ocr_result_empty_text_is_truthy_when_pages_zero() -> None:
    r = OcrResult(text="", pages=0, backend="fake")
    assert r.text == ""
    assert r.degraded is True


# --------------------------------------------------------------------------- #
# FakeOcrBackend
# --------------------------------------------------------------------------- #


def test_fake_backend_returns_seeded_text() -> None:
    fake = FakeOcrBackend()
    fake.seed_text("扫描件内容 AL-01")
    result = fake.ocr(b"\x89PNG fake bytes")
    assert "AL-01" in result.text


def test_fake_backend_no_seed_returns_empty() -> None:
    fake = FakeOcrBackend()
    result = fake.ocr(b"")
    assert result.text == ""
    assert result.degraded is True


# --------------------------------------------------------------------------- #
# OcrParser — section extraction
# --------------------------------------------------------------------------- #


def test_parser_produces_sections_from_ocr_text() -> None:
    fake = FakeOcrBackend()
    fake.seed_text("第一页告警 AL-01\n第二页告警 AL-02")
    parser = OcrParser(backend=fake)
    sections = parser.parse(b"fake-image-bytes")
    assert len(sections) >= 1
    joined = " ".join(b for s in sections for b in s.blocks)
    assert "AL-01" in joined
    assert "AL-02" in joined


def test_parser_empty_ocr_returns_no_sections() -> None:
    fake = FakeOcrBackend()
    parser = OcrParser(backend=fake)
    sections = parser.parse(b"")
    assert sections == []


def test_parser_accepts_bytes_and_path(tmp_path) -> None:
    fake = FakeOcrBackend()
    fake.seed_text("hello")
    parser = OcrParser(backend=fake)
    # path
    p = tmp_path / "scan.png"
    p.write_bytes(b"fake")
    sections = parser.parse(str(p))
    assert any("hello" in b for s in sections for b in s.blocks)
    # bytes
    sections2 = parser.parse(b"fake")
    assert sections2


# --------------------------------------------------------------------------- #
# Degradation: no backend available
# --------------------------------------------------------------------------- #


def test_parser_disabled_backend_returns_empty_with_warning() -> None:
    """OCR_BACKEND=disabled → empty sections, parser.degraded True."""
    from ai_employee.ingestion_worker.ocr import DisabledOcrBackend

    parser = OcrParser(backend=DisabledOcrBackend())
    sections = parser.parse(b"anything")
    assert sections == []
    assert parser.degraded is True


# --------------------------------------------------------------------------- #
# build_ocr_backend factory
# --------------------------------------------------------------------------- #


def test_build_backend_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_employee.ingestion_worker.ocr import DisabledOcrBackend

    monkeypatch.setenv("OCR_BACKEND", "disabled")
    backend = build_ocr_backend()
    assert isinstance(backend, DisabledOcrBackend)


def test_build_backend_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_BACKEND", "tesseract")
    backend = build_ocr_backend()
    assert isinstance(backend, TesseractOcrBackend)


def test_build_backend_rapidocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_BACKEND", "rapidocr")
    backend = build_ocr_backend()
    assert isinstance(backend, RapidOcrBackend)


def test_build_backend_defaults_to_disabled_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_employee.ingestion_worker.ocr import DisabledOcrBackend

    monkeypatch.delenv("OCR_BACKEND", raising=False)
    backend = build_ocr_backend()
    # Default is disabled (no OCR deps in CI); explicit opt-in required.
    assert isinstance(backend, DisabledOcrBackend)


# --------------------------------------------------------------------------- #
# Backend availability probing
# --------------------------------------------------------------------------- #


def test_rapidocr_backend_availability_flag() -> None:
    """The backend exposes ``available`` so callers can probe without crashing."""
    backend = RapidOcrBackend()
    assert backend.available in (True, False)


def test_tesseract_backend_availability_flag() -> None:
    backend = TesseractOcrBackend()
    assert backend.available in (True, False)


def test_rapidocr_unavailable_degrades_to_empty() -> None:
    """If rapidocr isn't importable, ocr() returns degraded empty result."""
    backend = RapidOcrBackend()
    if backend.available:
        pytest.skip("rapidocr installed; cannot test the unavailable path")
    result = backend.ocr(b"fake")
    assert result.text == ""
    assert result.degraded is True


def test_tesseract_unavailable_degrades_to_empty() -> None:
    backend = TesseractOcrBackend()
    if backend.available:
        pytest.skip("tesseract installed; cannot test the unavailable path")
    result = backend.ocr(b"fake")
    assert result.text == ""
    assert result.degraded is True


# --------------------------------------------------------------------------- #
# Integration: PdfParser falls back to OCR for image-only pages
# --------------------------------------------------------------------------- #


def test_pdf_parser_uses_ocr_for_image_only_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PDF with no extractable text triggers OCR fallback."""
    from ai_employee.ingestion_worker.parsers import PdfParser

    fake = FakeOcrBackend()
    fake.seed_text("OCR recovered text AL-99")
    parser = PdfParser(ocr_backend=fake)
    # _extract_text returns [(page_num, text)]; empty text → OCR path.
    monkeypatch.setattr(parser, "_extract_text", lambda src: [(1, "")])
    # Stub the page-render step so OCR gets bytes without a real PDF.
    monkeypatch.setattr(parser, "_ocr_page", lambda data, pn: "OCR recovered text AL-99")
    sections = parser.parse(b"%PDF-1.4 fake")
    joined = " ".join(b for s in sections for b in s.blocks)
    assert "AL-99" in joined


def test_pdf_parser_skips_ocr_when_text_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the PDF has extractable text, OCR is never called."""
    from ai_employee.ingestion_worker.parsers import PdfParser

    fake = FakeOcrBackend()
    fake.seed_text("SHOULD NOT APPEAR")
    parser = PdfParser(ocr_backend=fake)
    monkeypatch.setattr(
        parser, "_extract_text", lambda src: [(1, "real extracted text")],
    )
    monkeypatch.setattr(parser, "_ocr_page", lambda data, pn: "SHOULD NOT APPEAR")
    sections = parser.parse(b"%PDF-1.4 fake")
    joined = " ".join(b for s in sections for b in s.blocks)
    assert "real extracted text" in joined
    assert "SHOULD NOT APPEAR" not in joined
