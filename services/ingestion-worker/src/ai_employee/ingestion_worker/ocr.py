"""OCR backends for scanned-document ingestion (R17-1 / spec §5.2).

Pluggable OCR so the platform can extract text from scanned PDFs and
images without a hard dependency on a system binary at import time.

Backends:

* :class:`FakeOcrBackend` — deterministic, for tests.
* :class:`RapidOcrBackend` — wraps ``rapidocr_onnxruntime`` (preferred;
  CPU-friendly, pip-installable, no system binary).
* :class:`TesseractOcrBackend` — shells out to the ``tesseract`` CLI
  (fallback when rapidocr isn't available).
* :class:`DisabledOcrBackend` — no-op; returns empty text + degraded.
  Default when ``OCR_BACKEND`` is unset (CI has no OCR deps).

Env: ``OCR_BACKEND`` = ``rapidocr`` | ``tesseract`` | ``disabled`` (default).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ai_employee.ingestion_worker.parsers import ParsedSection

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class OcrResult:
    text: str
    pages: int = 0
    backend: str = "unknown"

    @property
    def degraded(self) -> bool:
        """True when OCR produced no usable text (empty / backend missing)."""
        return not self.text.strip()


# --------------------------------------------------------------------------- #
# Backend protocol
# --------------------------------------------------------------------------- #


class OcrBackend(Protocol):
    name: str
    available: bool

    def ocr(self, source: bytes | str) -> OcrResult: ...


# --------------------------------------------------------------------------- #
# Fake (tests)
# --------------------------------------------------------------------------- #


class FakeOcrBackend:
    """Deterministic OCR backend for tests; returns seeded text."""

    name = "fake"
    available = True

    def __init__(self) -> None:
        self._text = ""

    def seed_text(self, text: str) -> None:
        self._text = text

    def ocr(self, source: bytes | str) -> OcrResult:
        return OcrResult(text=self._text, pages=1 if self._text else 0, backend="fake")


# --------------------------------------------------------------------------- #
# Disabled (no-op)
# --------------------------------------------------------------------------- #


class DisabledOcrBackend:
    """No-op backend; the default when OCR deps are absent.

    Returns an empty :class:`OcrResult` so the parser can mark the doc
    as parse_failed rather than crashing.
    """

    name = "disabled"
    available = False

    def ocr(self, source: bytes | str) -> OcrResult:
        return OcrResult(text="", pages=0, backend="disabled")


# --------------------------------------------------------------------------- #
# RapidOCR
# --------------------------------------------------------------------------- #


class RapidOcrBackend:
    """Wraps ``rapidocr_onnxruntime`` (CPU-friendly, pip-installable)."""

    name = "rapidocr"
    available = False

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]

            self._engine = RapidOCR()
            self.available = True
        except Exception:
            self._engine = None  # type: ignore[assignment]
            self.available = False

    def ocr(self, source: bytes | str) -> OcrResult:
        if not self.available:
            return OcrResult(text="", pages=0, backend="rapidocr")
        try:
            result, _elapsed = self._engine(source)  # type: ignore[misc]
        except Exception:
            return OcrResult(text="", pages=0, backend="rapidocr")
        if not result:
            return OcrResult(text="", pages=0, backend="rapidocr")
        # result is a list of [box, text, score] triples.
        lines = [item[1] for item in result if item and len(item) > 1]
        return OcrResult(text="\n".join(lines), pages=1, backend="rapidocr")


# --------------------------------------------------------------------------- #
# Tesseract
# --------------------------------------------------------------------------- #


class TesseractOcrBackend:
    """Shells out to the ``tesseract`` CLI."""

    name = "tesseract"
    available = False

    def __init__(self) -> None:
        self.available = shutil.which("tesseract") is not None

    def ocr(self, source: bytes | str) -> OcrResult:
        if not self.available:
            return OcrResult(text="", pages=0, backend="tesseract")
        try:
            proc = subprocess.run(
                ["tesseract", "-", "-", "--psm", "6"],
                input=_to_bytes(source),
                capture_output=True,
                timeout=60,
                check=False,
            )
        except Exception:
            return OcrResult(text="", pages=0, backend="tesseract")
        text = proc.stdout.decode("utf-8", errors="replace")
        return OcrResult(text=text, pages=1, backend="tesseract")


def _to_bytes(source: bytes | str) -> bytes:
    if isinstance(source, bytes):
        return source
    if os.path.exists(source):
        with open(source, "rb") as fh:
            return fh.read()
    return source.encode("utf-8")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_ocr_backend() -> OcrBackend:
    """Pick an OCR backend from ``OCR_BACKEND`` env (default disabled)."""
    name = os.getenv("OCR_BACKEND", "disabled").strip().lower()
    if name == "rapidocr":
        return RapidOcrBackend()
    if name == "tesseract":
        return TesseractOcrBackend()
    return DisabledOcrBackend()


# --------------------------------------------------------------------------- #
# OcrParser
# --------------------------------------------------------------------------- #


class OcrParser:
    """Parse a scanned image / image-only PDF into :class:`ParsedSection`s.

    Delegates text extraction to the injected :class:`OcrBackend`.  The
    parser splits the OCR text into blocks on newlines (each non-empty
    line is one block) and groups them into a single section.
    """

    def __init__(self, backend: OcrBackend | None = None) -> None:
        self.backend = backend or build_ocr_backend()
        self.degraded = False

    def parse(self, source: bytes | str) -> list[ParsedSection]:
        result = self.backend.ocr(source)
        if result.degraded:
            self.degraded = True
            return []
        blocks = [line.strip() for line in result.text.splitlines() if line.strip()]
        if not blocks:
            self.degraded = True
            return []
        return [ParsedSection(section_path="ocr", blocks=blocks)]


__all__ = [
    "DisabledOcrBackend",
    "FakeOcrBackend",
    "OcrBackend",
    "OcrParser",
    "OcrResult",
    "RapidOcrBackend",
    "TesseractOcrBackend",
    "build_ocr_backend",
]
