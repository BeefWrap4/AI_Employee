"""Qwen-VL-OCR backend tests (R17-4 / spec §5.2).

Qwen-VL-OCR is a multimodal vision-language model.  When hosted on
SiliconFlow, OCR works by sending a chat-completions request whose user
message carries the image (as a base64 ``data:`` URL) plus an
"extract all text" instruction.  The :class:`QwenVlOcrBackend` wraps
that call and parses the returned assistant content into an
:class:`OcrResult`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from ai_employee.ingestion_worker.ocr import (
    OcrResult,
    QwenVlOcrBackend,
    build_ocr_backend,
)

# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_qwen_vl_ocr_backend_defaults() -> None:
    b = QwenVlOcrBackend(api_key="sk-x")
    assert "siliconflow.cn" in b.base_url
    assert b.api_key == "sk-x"
    assert "Qwen" in b.model  # Qwen-VL family


def test_qwen_vl_ocr_backend_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-env")
    b = QwenVlOcrBackend()
    assert b.api_key == "sk-env"


def test_qwen_vl_ocr_backend_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    monkeypatch.setenv("SILICONFLOW_OCR_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct")
    b = QwenVlOcrBackend()
    assert b.model == "Qwen/Qwen2.5-VL-7B-Instruct"


def test_qwen_vl_ocr_backend_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")
    monkeypatch.setenv("SILICONFLOW_BASE_URL", "http://localhost:9999/v1")
    b = QwenVlOcrBackend()
    assert b.base_url == "http://localhost:9999/v1"


def test_qwen_vl_ocr_backend_has_descriptive_name() -> None:
    b = QwenVlOcrBackend(api_key="sk-x")
    assert "qwen-vl" in b.name.lower() or "ocr" in b.name.lower()


# --------------------------------------------------------------------------- #
# Availability flag
# --------------------------------------------------------------------------- #


def test_qwen_vl_ocr_backend_available_with_key() -> None:
    b = QwenVlOcrBackend(api_key="sk-x")
    assert b.available is True


def test_qwen_vl_ocr_backend_unavailable_without_key() -> None:
    b = QwenVlOcrBackend(api_key="")
    assert b.available is False


# --------------------------------------------------------------------------- #
# HTTP request shape (mocked)
# --------------------------------------------------------------------------- #


def test_ocr_sends_multimodal_request_with_image_and_prompt() -> None:
    """The backend POSTs a chat-completions with an image_url + text."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {
                "model": "Qwen/Qwen2.5-VL-72B-Instruct",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "告警码 AL-01"}},
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }

    def fake_post(url: str, **kwargs) -> _Resp:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        captured["json"] = kwargs.get("json") or {}
        captured["timeout"] = kwargs.get("timeout")
        return _Resp()

    with patch("httpx.post", new=fake_post):
        b = QwenVlOcrBackend(api_key="sk-sf")
        result = b.ocr(b"\x89PNG fake image bytes")

    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-sf"
    body = captured["json"]
    assert body["model"] == b.model
    # Multimodal: user message content is a list with image + text parts.
    msg = body["messages"][0]
    assert msg["role"] == "user"
    parts = msg["content"]
    assert isinstance(parts, list)
    assert any(p.get("type") == "image_url" for p in parts)
    assert any(p.get("type") == "text" for p in parts)
    # The image part carries a base64 data URL.
    img_part = next(p for p in parts if p.get("type") == "image_url")
    assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
    # Result parsed from assistant content.
    assert isinstance(result, OcrResult)
    assert result.text == "告警码 AL-01"
    assert result.backend == "qwen_vl_ocr"
    assert result.pages == 1


def test_ocr_accepts_file_path(tmp_path) -> None:
    """A file path is read and base64-encoded."""
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG fake")

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "OCR text"}}]}

    with patch("httpx.post", return_value=_Resp()):
        b = QwenVlOcrBackend(api_key="sk-x")
        result = b.ocr(str(img))
    assert result.text == "OCR text"


def test_ocr_prompt_instructions_present() -> None:
    """The text part must carry an explicit 'extract text' instruction."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "x"}}]}

    def fake_post(url: str, **kwargs) -> _Resp:
        captured["json"] = kwargs.get("json") or {}
        return _Resp()

    with patch("httpx.post", new=fake_post):
        b = QwenVlOcrBackend(api_key="sk-x")
        b.ocr(b"fake")
    parts = captured["json"]["messages"][0]["content"]
    text_part = next(p for p in parts if p.get("type") == "text")
    # Instruction mentions extracting / recognizing text.
    assert any(
        kw in text_part["text"].lower()
        for kw in ("extract", "ocr", "recognize", "识别", "提取")
    )


# --------------------------------------------------------------------------- #
# Error / fallback paths
# --------------------------------------------------------------------------- #


def test_ocr_falls_back_on_http_error() -> None:
    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self) -> dict:
            return {}

    with patch("httpx.post", return_value=_Resp()):
        b = QwenVlOcrBackend(api_key="bad")
        result = b.ocr(b"fake")
    assert result.text == ""
    assert result.degraded is True


def test_ocr_falls_back_on_transport_error() -> None:
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    with patch("httpx.post", new=boom):
        b = QwenVlOcrBackend(api_key="sk-x")
        result = b.ocr(b"fake")
    assert result.text == ""
    assert result.degraded is True


def test_ocr_falls_back_when_api_key_missing() -> None:
    """No key → backend.available False → degraded empty result, no network."""
    b = QwenVlOcrBackend(api_key="")
    result = b.ocr(b"fake")
    assert result.text == ""
    assert result.degraded is True


def test_ocr_falls_back_on_empty_content() -> None:
    """If the model returns an empty content string, mark degraded."""

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"choices": [{"message": {"content": ""}}]}

    with patch("httpx.post", return_value=_Resp()):
        b = QwenVlOcrBackend(api_key="sk-x")
        result = b.ocr(b"fake")
    assert result.text == ""
    assert result.degraded is True


def test_ocr_falls_back_on_missing_choices() -> None:
    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"choices": []}

    with patch("httpx.post", return_value=_Resp()):
        b = QwenVlOcrBackend(api_key="sk-x")
        result = b.ocr(b"fake")
    assert result.text == ""


# --------------------------------------------------------------------------- #
# Factory selection
# --------------------------------------------------------------------------- #


def test_build_backend_qwen_vl_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_BACKEND", "qwen_vl_ocr")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    b = build_ocr_backend()
    assert isinstance(b, QwenVlOcrBackend)


def test_build_backend_qwen_vl_ocr_without_key_still_returns_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting qwen_vl_ocr without a key returns the backend (marked
    unavailable); build_ocr_backend doesn't crash — degrade at call time."""
    monkeypatch.setenv("OCR_BACKEND", "qwen_vl_ocr")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    b = build_ocr_backend()
    assert isinstance(b, QwenVlOcrBackend)
    assert b.available is False


# --------------------------------------------------------------------------- #
# Integration: OcrParser → QwenVlOcrBackend
# --------------------------------------------------------------------------- #


def test_ocr_parser_uses_qwen_vl_backend() -> None:
    from ai_employee.ingestion_worker.ocr import OcrParser

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "第1行告警 AL-01\n第2行 AL-02"}}]}

    with patch("httpx.post", return_value=_Resp()):
        backend = QwenVlOcrBackend(api_key="sk-x")
        parser = OcrParser(backend=backend)
        sections = parser.parse(b"\x89PNG fake")
    assert len(sections) == 1
    joined = " ".join(sections[0].blocks)
    assert "AL-01" in joined
    assert "AL-02" in joined
