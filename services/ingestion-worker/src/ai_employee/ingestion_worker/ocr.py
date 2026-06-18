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
from typing import Any, Protocol

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
# Qwen-VL-OCR (Alibaba Bailian / 百炼 dedicated OCR model)
# --------------------------------------------------------------------------- #

# Default: Alibaba Cloud Bailian "qwen-vl-ocr" — a purpose-built OCR VLM.
# Called via the dashscope OpenAI-compatible chat-completions endpoint.
_DEFAULT_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_OCR_MODEL = "qwen-vl-ocr"
# General VLM models (e.g. qwen-vl-max) need an explicit OCR instruction;
# the dedicated "qwen-vl-ocr" model is itself an OCR engine and prefers
# an image-only message.
_OCR_PROMPT = (
    "You are an OCR engine. Extract ALL visible text from the image "
    "verbatim, preserving line breaks. Output only the recognized text, "
    "no commentary. (识别并提取图片中所有可见文字，保留换行，仅输出文本。)"
)
_DEDICATED_OCR_MODEL_IDS = frozenset({"qwen-vl-ocr", "qwen-vl-ocr-latest"})


def _b64_data_url(data: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


class QwenVlOcrBackend:
    """OCR via the Qwen-VL-OCR model on Alibaba Cloud Bailian (百炼).

    Calls the dashscope OpenAI-compatible chat-completions endpoint with
    the image as a base64 ``data:`` URL.  The dedicated ``qwen-vl-ocr``
    model is itself an OCR engine — no prompt is sent (the message is
    image-only).  For a general VLM (e.g. ``qwen-vl-max``,
    ``Qwen/Qwen2.5-VL-72B-Instruct``) the backend auto-adds an explicit
    extract-text instruction.

    Auth: ``DASHSCOPE_API_KEY`` (Bailian default).  Falls back to
    ``SILICONFLOW_API_KEY`` so a single key lights up both chat (R15)
    and OCR.  To target SiliconFlow explicitly, set
    ``SILICONFLOW_BASE_URL`` + ``QWEN_VL_OCR_MODEL`` together.

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
        # Resolve auth: explicit arg > DASHSCOPE_API_KEY > SILICONFLOW_API_KEY.
        resolved_key = (
            api_key
            if api_key is not None
            else os.getenv("DASHSCOPE_API_KEY") or os.getenv("SILICONFLOW_API_KEY", "")
        )
        self.api_key = resolved_key
        # Resolve base URL: explicit arg > DASHSCOPE_BASE_URL >
        # SILICONFLOW_BASE_URL > Bailian default.
        self.base_url = (
            base_url
            or os.getenv("DASHSCOPE_BASE_URL")
            or os.getenv("SILICONFLOW_BASE_URL", _DEFAULT_BAILIAN_BASE_URL)
        ).rstrip("/")
        # Resolve model: explicit arg > QWEN_VL_OCR_MODEL > default.
        self.model = (
            model
            or os.getenv("QWEN_VL_OCR_MODEL")
            or _DEFAULT_OCR_MODEL
        )
        self.timeout = timeout_seconds
        # Available iff a key is configured; the VLM is always hosted.
        self.available = bool(self.api_key)

    def _is_dedicated_ocr(self) -> bool:
        return self.model in _DEDICATED_OCR_MODEL_IDS

    def ocr(self, source: bytes | str) -> OcrResult:
        if not self.available:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        data = _to_bytes(source)
        if not data:
            return OcrResult(text="", pages=0, backend="qwen_vl_ocr")
        import httpx

        user_content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": _b64_data_url(data)}},
        ]
        # General VLMs need an OCR instruction; the dedicated model is
        # itself an OCR engine and is fed image-only.
        if not self._is_dedicated_ocr():
            user_content.append({"type": "text", "text": _OCR_PROMPT})

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": user_content}],
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
