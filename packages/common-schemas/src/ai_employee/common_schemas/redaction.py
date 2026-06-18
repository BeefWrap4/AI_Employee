"""Sensitive field redaction (spec §8 / §4.6 governance).

Masks PII and secret-shaped substrings before persistence in QA logs,
ticket writebacks, RCA reports, and approval tasks.  Pattern set is
configurable via ``RedactionConfig``; defaults cover phone numbers,
emails, Chinese ID numbers (18-digit), IPv4, and bearer tokens.

A redacted token is replaced with a stable placeholder so reviewers can
still tell that *something* was removed without seeing the original.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class RedactionConfig:
    """Which patterns to redact and what placeholder to use."""

    redact_phone: bool = True
    redact_email: bool = True
    redact_id_card: bool = True
    redact_ip: bool = True
    redact_token: bool = True
    placeholder: str = "***"
    custom_patterns: list[str] = field(default_factory=list)


_PHONE_RE = re.compile(
    r"(?:"
    r"(?:\+?86[-\s]?)?1[3-9]\d{9}"  # China mobile (10-11 digits)
    r"|"
    r"\+\d{1,3}[-\s]\d{2,4}[-\s]\d{3,5}[-\s]\d{3,5}"  # international with dashes
    r"|"
    r"\+\d{10,15}"  # international compact (E.164)
    r")"
    r"\b"
)
_EMAIL_RE = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b")
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")  # China resident ID
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{16,}")


def _compiled_patterns(cfg: RedactionConfig) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    if cfg.redact_phone:
        patterns.append(_PHONE_RE)
    if cfg.redact_email:
        patterns.append(_EMAIL_RE)
    if cfg.redact_id_card:
        patterns.append(_ID_CARD_RE)
    if cfg.redact_ip:
        patterns.append(_IPV4_RE)
    if cfg.redact_token:
        patterns.append(_BEARER_RE)
    for p in cfg.custom_patterns:
        patterns.append(re.compile(p))
    return patterns


def redact_text(text: str, cfg: RedactionConfig | None = None) -> str:
    """Return ``text`` with sensitive substrings replaced by placeholders."""
    if not text:
        return text
    cfg = cfg or RedactionConfig()
    out = text
    for pat in _compiled_patterns(cfg):
        out = pat.sub(cfg.placeholder, out)
    return out


def redact_dict(data: dict, *, fields: list[str], cfg: RedactionConfig | None = None) -> dict:
    """Return a shallow copy of ``data`` with selected fields redacted."""
    cfg = cfg or RedactionConfig()
    out = dict(data)
    for field_name in fields:
        if field_name in out and isinstance(out[field_name], str):
            out[field_name] = redact_text(out[field_name], cfg)
    return out


__all__ = [
    "RedactionConfig",
    "redact_dict",
    "redact_text",
]
