"""Query normalization — extract telecom entities from a natural-language
question (spec §5.4 stage 1: Query Normalize).

Identifies alarm codes, vendors, NE IDs, cell IDs, and metric names so
the downstream BM25/vector recall can match on normalised terms even
when the user writes informal phrasing.

Zero-dependency: uses a curated telecom regex + keyword dictionary.  A
production deployment could swap in an LLM-typed entity recogniser behind
the same :func:`normalize_query` interface.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Alarm-code patterns: upper-snake or UPPER_WITH_DIGITS tokens, e.g.
# LINK_DEGRADE, RRC_SETUP_FAIL_HIGH, ALM_2541.
_ALARM_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,}(?:_[A-Z0-9]+)*)\b")
# NE / cell / site identifiers: NE-001, CELL-001, SITE-001, gNB-123.
_ENTITY_RE = re.compile(r"\b(NE|CELL|SITE|gNB|ENB|NRCELL|SECTOR)[-_]?(\d+)\b", re.IGNORECASE)
# Known vendors and network types (lowercased match).
_VENDORS = {"huawei", "zte", "nokia", "ericsson", "大唐", "烽火", "华为", "中兴", "诺基亚"}
_NETWORK_TYPES = {"4g", "5g", "lte", "nr", "transport", "传输", "无线"}
# Common KPI / metric terms.
_METRICS = {
    "rrc", "rrc setup", "rrc 建立失败", "setup failure", "接通率", "掉话率",
    "ber", "crc", "光功率", "误码", "throughput", "吞吐", "prb", "cpu", "memory",
}


@dataclass
class QueryEntities:
    alarm_codes: list[str] = field(default_factory=list)
    ne_ids: list[str] = field(default_factory=list)
    cell_ids: list[str] = field(default_factory=list)
    site_ids: list[str] = field(default_factory=list)
    vendors: list[str] = field(default_factory=list)
    network_types: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)


def extract_entities(question: str) -> QueryEntities:
    """Extract telecom entities from a question."""
    text = question or ""
    ents = QueryEntities()

    for match in _ALARM_CODE_RE.findall(text):
        # Filter out common false positives (single English words like "SOP").
        if match.upper() in {"SOP", "PDF", "API", "MCP", "HTTP", "JSON"}:
            continue
        if match not in ents.alarm_codes:
            ents.alarm_codes.append(match)

    for prefix, num in _ENTITY_RE.findall(text):
        token = f"{prefix.upper()}-{num}"
        kind = prefix.upper()
        if kind == "NE":
            ents.ne_ids.append(token)
        elif kind == "CELL":
            ents.cell_ids.append(token)
        elif kind == "SITE":
            ents.site_ids.append(token)
        else:
            ents.ne_ids.append(token)

    lower = text.lower()
    for v in _VENDORS:
        if v.lower() in lower and v not in ents.vendors:
            ents.vendors.append(v)
    for n in _NETWORK_TYPES:
        if n.lower() in lower and n not in ents.network_types:
            ents.network_types.append(n)
    for m in _METRICS:
        if m.lower() in lower and m not in ents.metrics:
            ents.metrics.append(m)

    return ents


def normalize_query(question: str) -> str:
    """Return a normalised keyword string combining the original question
    with extracted entities, for use as BM25 / FTS5 input.

    Keeps the original text (so phrasing is preserved for vector recall)
    and appends extracted entities as additional tokens so an alarm code
    written inline (e.g. "RRC_SETUP_FAIL_HIGH") is guaranteed to match
    a chunk indexed under that code.
    """
    ents = extract_entities(question)
    extra: list[str] = []
    extra.extend(ents.alarm_codes)
    extra.extend(ents.ne_ids)
    extra.extend(ents.cell_ids)
    extra.extend(ents.site_ids)
    extra.extend(ents.vendors)
    extra.extend(ents.network_types)
    extra.extend(ents.metrics)
    # Dedup while preserving order; drop tokens already present verbatim.
    seen: set[str] = set()
    parts: list[str] = []
    for token in extra:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(token)
    if not parts:
        return question
    return f"{question} {' '.join(parts)}"


__all__ = ["QueryEntities", "extract_entities", "normalize_query"]
