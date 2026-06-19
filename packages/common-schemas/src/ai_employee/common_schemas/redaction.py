"""Sensitive field redaction (spec §8 / §4.6 governance).

Masks PII and secret-shaped substrings before persistence in QA logs,
ticket writebacks, RCA reports, and approval tasks.  Pattern set is
configurable via ``RedactionConfig``; defaults cover phone numbers,
emails, Chinese ID numbers (18-digit), IPv4, IMSI (SIM identifiers),
and password-shaped field names (password/passwd/secret/token).

A redacted token is replaced with a stable placeholder so reviewers can
still tell that *something* was removed without seeing the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PASSWORD_PLACEHOLDER = "***REDACTED***"
_PASSWORD_FIELD_RE = re.compile(r"(?i)(password|passwd|secret|token)")


@dataclass
class RedactionConfig:
    """Which patterns to redact and what placeholder to use."""

    redact_phone: bool = True
    redact_email: bool = True
    redact_id_card: bool = True
    redact_ip: bool = True
    redact_token: bool = True
    redact_imsi: bool = True
    redact_password: bool = True
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
# IMSI = MCC (3 digits) + MNC (2-3 digits) + MSIN (up to 10 digits), 15 digits total.
# China carriers: 46000/46001/46002/46003/46005/46006/46007/46008/46009/46011 ...
_IMSI_RE = re.compile(r"(?<!\d)4600\d{11}(?!\d)")
# Generic IMSI shape (15 digits) — applied only when redact_imsi is on, to catch
# non-China carriers (e.g. 310260123456789).
_IMSI_GENERIC_RE = re.compile(r"(?<!\d)\d{15}(?!\d)")


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
    if cfg.redact_imsi:
        patterns.append(_IMSI_RE)
        patterns.append(_IMSI_GENERIC_RE)
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


def _is_password_field(name: str) -> bool:
    return bool(_PASSWORD_FIELD_RE.search(name))


def redact_dict(data: dict, *, fields: list[str], cfg: RedactionConfig | None = None) -> dict:
    """Return a copy of ``data`` with selected fields redacted.

    Behaviour:

    * Top-level keys in ``fields`` have their string values passed through
      :func:`redact_text`.
    * When a value is a dict, the function recurses and applies the
      same field list to the nested structure.
    * When a value is a list, the function recurses into each element
      that is itself a dict.
    * When :attr:`RedactionConfig.redact_password` is enabled, dict keys
      matching ``password|passwd|secret|token`` (case-insensitive) are
      replaced with the constant ``***REDACTED***`` placeholder — these
      whole-value replacements take precedence over text redaction.
    """
    cfg = cfg or RedactionConfig()
    return _redact_value(data, fields=set(fields), cfg=cfg, _root=True)


def _redact_value(value, *, fields: set[str], cfg: RedactionConfig, _root: bool):
    if isinstance(value, dict):
        out: dict = {}
        for key, val in value.items():
            if cfg.redact_password and isinstance(val, str) and _is_password_field(key):
                out[key] = _PASSWORD_PLACEHOLDER
                continue
            if key in fields and isinstance(val, str):
                out[key] = redact_text(val, cfg)
                continue
            out[key] = _redact_value(val, fields=fields, cfg=cfg, _root=False)
        return out
    if isinstance(value, list):
        return [_redact_value(item, fields=fields, cfg=cfg, _root=False) for item in value]
    return value


__all__ = [
    "RedactionConfig",
    "redact_dict",
    "redact_text",
]
