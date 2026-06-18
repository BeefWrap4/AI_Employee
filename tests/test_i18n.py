"""Multi-language i18n tests for knowledge-api.

Detect the question language (zh / en) and optionally translate the
answer to a target locale.  Locale can be supplied via:
  - explicit ``locale`` arg (highest priority)
  - ``X-Locale`` header
  - auto-detection from the question (fallback)
"""
from __future__ import annotations

from ai_employee.knowledge_api.i18n import (
    SUPPORTED_LOCALES,
    Locale,
    detect_locale,
    parse_locale_header,
    resolve_locale,
    set_translator,
    translate_text,
)

# --------------------------------------------------------------------------- #
# detect_locale
# --------------------------------------------------------------------------- #


def test_detect_locale_chinese() -> None:
    assert detect_locale("什么是 RRC 建立失败？") == "zh-CN"


def test_detect_locale_english() -> None:
    assert detect_locale("What is RRC setup failure?") == "en-US"


def test_detect_locale_empty_string_defaults_to_en() -> None:
    assert detect_locale("") == "en-US"


def test_detect_locale_mixed_chinese_heavy_is_chinese() -> None:
    assert detect_locale("北京站点 PRB 利用率告警") == "zh-CN"


def test_detect_locale_pure_punctuation() -> None:
    assert detect_locale("???!!") == "en-US"


# --------------------------------------------------------------------------- #
# parse_locale_header
# --------------------------------------------------------------------------- #


def test_parse_locale_header_simple() -> None:
    assert parse_locale_header("en-US") == "en-US"


def test_parse_locale_header_with_charset() -> None:
    assert parse_locale_header("zh-CN, charset=utf-8") == "zh-CN"


def test_parse_locale_header_quality_weights() -> None:
    parsed = parse_locale_header("zh-CN;q=0.9,en-US;q=0.8")
    assert parsed == "zh-CN"


def test_parse_locale_header_unsupported_falls_back_to_default() -> None:
    assert parse_locale_header("fr-FR") is None


def test_parse_locale_header_empty_returns_none() -> None:
    assert parse_locale_header("") is None
    assert parse_locale_header(None) is None


# --------------------------------------------------------------------------- #
# resolve_locale
# --------------------------------------------------------------------------- #


def test_resolve_locale_explicit_arg_wins() -> None:
    locale = resolve_locale(
        explicit="en-US",
        header="zh-CN",
        question="什么是 RRC？",
    )
    assert locale == "en-US"


def test_resolve_locale_header_used_when_no_explicit() -> None:
    locale = resolve_locale(
        explicit=None,
        header="en-US",
        question="什么是 RRC？",
    )
    assert locale == "en-US"


def test_resolve_locale_detects_from_question() -> None:
    locale = resolve_locale(
        explicit=None, header=None,
        question="什么是 RRC 建立失败？",
    )
    assert locale == "zh-CN"


def test_resolve_locale_unsupported_header_falls_back_to_detection() -> None:
    locale = resolve_locale(
        explicit=None, header="fr-FR",
        question="什么是 RRC？",
    )
    assert locale == "zh-CN"


def test_supported_locales_constant() -> None:
    assert "zh-CN" in SUPPORTED_LOCALES
    assert "en-US" in SUPPORTED_LOCALES


def test_locale_dataclass() -> None:
    loc = Locale(code="en-US", label="English (US)")
    assert loc.code == "en-US"
    assert "English" in loc.label


# --------------------------------------------------------------------------- #
# translate_text
# --------------------------------------------------------------------------- #


def test_translate_text_no_translator_returns_tagged_stub() -> None:
    set_translator(None)
    out = translate_text("hello", source_locale="en-US", target_locale="zh-CN")
    assert "zh-CN" in out
    assert "hello" in out


def test_translate_text_same_locale_returns_unchanged() -> None:
    out = translate_text("hello", source_locale="en-US", target_locale="en-US")
    assert out == "hello"


def test_translate_text_empty_input_returns_empty() -> None:
    assert translate_text("", source_locale="en-US", target_locale="zh-CN") == ""


def test_translate_text_with_translator_uses_callback() -> None:
    set_translator(lambda text, s, t: f"TRANSLATED({t}):{text}")
    out = translate_text("hello", source_locale="en-US", target_locale="zh-CN")
    assert out == "TRANSLATED(zh-CN):hello"
    set_translator(None)  # reset
