"""Qwen-VL-OCR backend tests (R17-4 / spec §5.2).

The :class:`QwenVlOcrBackend` calls Alibaba Cloud Bailian (百炼) `qwen-vl-ocr`,
a purpose-built OCR vision-language model, via the dashscope
OpenAI-compatible endpoint.  It sends a chat-completions request whose
user message carries the image (base64 ``data:`` URL); the dedicated
``qwen-vl-ocr`` model needs no OCR prompt.  For a general VLM (e.g.
Qwen2.5-VL-Instruct) the prompt is auto-added.

Auth: ``DASHSCOPE_API_KEY`` (Bailian default).  The backend can also be
pointed at SiliconFlow's hosted Qwen-VL by setting ``SILICONFLOW_API_KEY``
+ ``SILICONFLOW_BASE_URL`` explicitly.
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
# Construction — Bailian (百炼) defaults
# --------------------------------------------------------------------------- #


def test_qwen_vl_ocr_backend_defaults_to_bailian() -> None:
    b = QwenVlOcrBackend(api_key="sk-x")
    assert "dashscope.aliyuncs.com" in b.base_url
    assert b.api_key == "sk-x"
    # Default model id is the dedicated qwen-vl-ocr on Bailian.
    assert b.model == "qwen-vl-ocr"


def test_qwen_vl_ocr_backend_reads_dashscope_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    b = QwenVlOcrBackend()
    assert b.api_key == "sk-env"


def test_qwen_vl_ocr_backend_prefers_qwen_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QWEN_API_KEY is the project-wide Qwen auth env; it takes precedence
    over the more specific DASHSCOPE_API_KEY / SILICONFLOW_API_KEY."""
    monkeypatch.setenv("QWEN_API_KEY", "sk-qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-ds")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    b = QwenVlOcrBackend()
    assert b.api_key == "sk-qwen"


def test_qwen_vl_ocr_backend_qwen_api_key_with_dashscope_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QWEN_API_KEY (auth) + DASHSCOPE_BASE_URL (endpoint) compose cleanly."""
    monkeypatch.setenv("QWEN_API_KEY", "sk-qwen")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    b = QwenVlOcrBackend()
    assert b.api_key == "sk-qwen"
    assert "dashscope" in b.base_url


def test_qwen_vl_ocr_backend_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override to a general VLM (prompt auto-added for non-ocr models)."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-x")
    monkeypatch.setenv("QWEN_VL_OCR_MODEL", "qwen-vl-max")
    b = QwenVlOcrBackend()
    assert b.model == "qwen-vl-max"


def test_qwen_vl_ocr_backend_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-x")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "http://localhost:9999/v1")
    b = QwenVlOcrBackend()
    assert b.base_url == "http://localhost:9999/v1"


def test_qwen_vl_ocr_backend_can_target_siliconflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pointing at SiliconFlow's hosted Qwen-VL via explicit env."""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf")
    monkeypatch.setenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("QWEN_VL_OCR_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    b = QwenVlOcrBackend()
    assert b.api_key == "sk-sf"
    assert "siliconflow.cn" in b.base_url
    assert b.model == "Qwen/Qwen2.5-VL-72B-Instruct"


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


def test_ocr_sends_multimodal_request_with_image() -> None:
    """The backend POSTs a chat-completions with an image_url to Bailian."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {
                "model": "qwen-vl-ocr",
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
        b = QwenVlOcrBackend(api_key="sk-dashscope")
        result = b.ocr(b"\x89PNG fake image bytes")

    # Targets the dashscope compatible-mode chat-completions endpoint.
    assert "dashscope.aliyuncs.com" in captured["url"]
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-dashscope"
    body = captured["json"]
    assert body["model"] == "qwen-vl-ocr"
    # Multimodal: user message content is a list with an image part.
    msg = body["messages"][0]
    assert msg["role"] == "user"
    parts = msg["content"]
    assert isinstance(parts, list)
    assert any(p.get("type") == "image_url" for p in parts)
    img_part = next(p for p in parts if p.get("type") == "image_url")
    assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
    # Result parsed from assistant content.
    assert isinstance(result, OcrResult)
    assert result.text == "告警码 AL-01"
    assert result.backend == "qwen_vl_ocr"
    assert result.pages == 1


def test_ocr_dedicated_model_omits_prompt() -> None:
    """qwen-vl-ocr is a dedicated OCR model — no text prompt needed."""
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
        b = QwenVlOcrBackend(api_key="sk-x")  # default model qwen-vl-ocr
        b.ocr(b"\x89PNG fake")
    parts = captured["json"]["messages"][0]["content"]
    # Only the image part; no text prompt for the dedicated OCR model.
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"


def test_ocr_general_vlm_adds_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A general VLM (non-ocr model id) gets an explicit extract-text prompt."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "x"}}]}

    def fake_post(url: str, **kwargs) -> _Resp:
        captured["json"] = kwargs.get("json") or {}
        return _Resp()

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-x")
    monkeypatch.setenv("QWEN_VL_OCR_MODEL", "qwen-vl-max")  # general VLM
    with patch("httpx.post", new=fake_post):
        b = QwenVlOcrBackend()
        b.ocr(b"\x89PNG fake")
    parts = captured["json"]["messages"][0]["content"]
    text_part = next(p for p in parts if p.get("type") == "text")
    assert any(
        kw in text_part["text"].lower() for kw in ("extract", "ocr", "recognize", "识别", "提取")
    )


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
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-ds")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    b = build_ocr_backend()
    assert isinstance(b, QwenVlOcrBackend)
    assert b.api_key == "sk-ds"


def test_build_backend_qwen_vl_ocr_without_key_still_returns_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting qwen_vl_ocr without a key returns the backend (marked
    unavailable); build_ocr_backend doesn't crash — degrade at call time."""
    monkeypatch.setenv("OCR_BACKEND", "qwen_vl_ocr")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
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
