"""Multi-language i18n helpers for knowledge-api (spec §4.5).

Three concerns:

* :func:`detect_locale` — guess the question's language from character
  ranges (CJK block vs Latin).  Cheap and deterministic.
* :func:`parse_locale_header` — parse the standard ``Accept-Language``
  / ``X-Locale`` header into a supported locale (or ``None`` when
  unsupported / malformed).
* :func:`translate_text` — pass-through translator that returns the
  source unchanged when source == target.  The real LLM translation
  pass can be plugged in via :func:`set_translator`.

Resolution order (:func:`resolve_locale`):

1. ``explicit`` argument (programmatic override)
2. ``header`` value (HTTP-layer override)
3. Auto-detection from the question text
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

SUPPORTED_LOCALES: tuple[str, ...] = ("zh-CN", "en-US")
_DEFAULT_LOCALE = "en-US"

# CJK Unified Ideographs block — dominant range for Simplified Chinese.
_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_LOCALE_TAG_RE = re.compile(r"([a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})?)")


@dataclass(frozen=True)
class Locale:
    code: str
    label: str


_LOCALES: dict[str, Locale] = {
    "zh-CN": Locale("zh-CN", "简体中文"),
    "en-US": Locale("en-US", "English (US)"),
}


def detect_locale(text: str) -> str:
    """Pick the locale whose script dominates ``text``.

    Counts CJK characters vs Latin characters; ties or all-other
    characters default to ``en-US``.  Empty input also defaults to
    English.
    """
    if not text:
        return _DEFAULT_LOCALE
    cjk = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cjk == 0:
        return _DEFAULT_LOCALE
    if cjk >= latin:
        return "zh-CN"
    return _DEFAULT_LOCALE


def parse_locale_header(header: str | None) -> str | None:
    """Parse an ``Accept-Language`` / ``X-Locale`` header.

    Accepts both ``zh-CN`` (plain tag) and ``zh-CN;q=0.9,en-US;q=0.8``
    (RFC 7231 quality-weighted list).  Returns the highest-priority
    *supported* locale, or ``None`` when the header is missing,
    malformed, or only references unsupported locales.
    """
    if not header:
        return None
    # Pull all locale tags from the header (with their optional q-weights).
    candidates: list[tuple[float, str]] = []
    for match in _LOCALE_TAG_RE.finditer(header):
        tag = match.group(1)
        # Check for an explicit q-weight in the surrounding slice.
        q = 1.0
        end = match.end()
        q_match = re.search(r"q\s*=\s*([0-9.]+)", header[end:end + 16])
        if q_match:
            try:
                q = float(q_match.group(1))
            except ValueError:
                q = 1.0
        candidates.append((q, tag))
    if not candidates:
        return None
    # Sort by q desc; stable for equal q (insertion order).
    candidates.sort(key=lambda c: -c[0])
    for _q, tag in candidates:
        if tag in SUPPORTED_LOCALES:
            return tag
    return None


def resolve_locale(
    *,
    explicit: str | None,
    header: str | None,
    question: str,
) -> str:
    """Pick a locale using the documented precedence.

    Returns one of :data:`SUPPORTED_LOCALES` — guaranteed to never
    return ``None``.  Detection runs as a final fallback when neither
    the explicit arg nor the header resolves to a supported tag.
    """
    if explicit and explicit in SUPPORTED_LOCALES:
        return explicit
    from_header = parse_locale_header(header)
    if from_header is not None:
        return from_header
    return detect_locale(question)


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #

TranslatorFn = Callable[[str, str, str], str]
"""Signature: ``translator(text, source_locale, target_locale) -> str``."""

_translator: TranslatorFn | None = None


def set_translator(fn: TranslatorFn | None) -> None:
    """Register a translator callable (or clear it)."""
    global _translator
    _translator = fn


def translate_text(
    text: str,
    *,
    source_locale: str,
    target_locale: str,
) -> str:
    """Translate ``text`` from ``source_locale`` to ``target_locale``.

    When no translator is registered or the locales match, the input
    is returned unchanged so callers can use this unconditionally.
    """
    if source_locale == target_locale or not text:
        return text
    if _translator is None:
        # No provider — emit a tagged stub so callers can detect the
        # untranslated state without crashing.
        return f"[{target_locale}] {text}"
    return _translator(text, source_locale, target_locale)


def get_locale(code: str) -> Locale | None:
    return _LOCALES.get(code)


__all__ = [
    "SUPPORTED_LOCALES",
    "Locale",
    "TranslatorFn",
    "detect_locale",
    "get_locale",
    "parse_locale_header",
    "resolve_locale",
    "set_translator",
    "translate_text",
]
