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


# --------------------------------------------------------------------------- #
# Qwen-VL-OCR (multimodal VLM via SiliconFlow chat-completions)
# --------------------------------------------------------------------------- #

# Default Qwen-VL model id hosted on SiliconFlow.  The VL-Instruct family
# performs OCR well when prompted to extract text; override via
# ``SILICONFLOW_OCR_MODEL``.  (SiliconFlow also exposes a dedicated
# ``qwen-vl-ocr`` alias on some accounts — set the env to use it.)
_DEFAULT_QWEN_VL_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"
_OCR_PROMPT = (
    "You are an OCR engine. Extract ALL visible text from the image "
    "verbatim, preserving line breaks. Output only the recognized text, "
    "no commentary. (识别并提取图片中所有可见文字，保留换行，仅输出文本。)"
)


def _b64_data_url(data: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


class QwenVlOcrBackend:
    """OCR via the Qwen-VL multimodal model on SiliconFlow.

    Sends a chat-completions request whose user message carries the image
    (base64 ``data:`` URL) + an extract-text instruction, then parses the
    assistant's reply into an :class:`OcrResult`.  Reuses
    ``SILICONFLOW_API_KEY`` / ``SILICONFLOW_BASE_URL`` so the OCR backend
    lights up for free once the platform key is set.

    Degrades to an empty result on any error (missing key, HTTP failure,
    transport error, empty content) so ingestion never crashes.
    """

    name = "qwen_vl_ocr"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("SILICONFLOW_API_KEY", "")
        self.base_url = (
            base_url
            or os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("SILICONFLOW_OCR_MODEL")
            or _DEFAULT_QWEN_VL_MODEL
        )
        self.timeout = timeout_seconds
        # Available iff a key is configured; the VLM itself is always
        # hosted, so there's no local-dep probe like rapidocr/tesseract.
        self.available = bool(self.api_key)

    def ocr(self, source: bytes | str) -> OcrResult:
        if not self.available:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        data = _to_bytes(source)
        if not data:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        import httpx

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _b64_data_url(data)}},
                        {"type": "text", "text": _OCR_PROMPT},
                    ],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except Exception:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        if resp.status_code >= 400:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        try:
            body = resp.json() or {}
            choices = body.get("choices") or []
            if not choices:
                return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
            content = choices[0].get("message", {}).get("content", "") or ""
        except Exception:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        if not content.strip():
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        return OcrResult(text=content, pages=1, backend="qwen_vl_ocr")


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
    """Pick an OCR backend from ``OCR_BACKEND`` env (default disabled).

    Options: ``rapidocr`` | ``tesseract`` | ``qwen_vl_ocr`` | ``disabled``.
    ``qwen_vl_ocr`` uses the Qwen-VL multimodal model on SiliconFlow
    (needs ``SILICONFLOW_API_KEY``); the local-CPU backends need their
    own deps.
    """
    name = os.getenv("OCR_BACKEND", "disabled").strip().lower()
    if name == "rapidocr":
        return RapidOcrBackend()
    if name == "tesseract":
        return TesseractOcrBackend()
    if name in {"qwen_vl_ocr", "qwen-vl-ocr", "qwen_vl", "qwen2_vl_ocr"}:
        return QwenVlOcrBackend()
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
    "QwenVlOcrBackend",
    "RapidOcrBackend",
    "TesseractOcrBackend",
    "build_ocr_backend",
]
