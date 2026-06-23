"""R33-E: pin the skip-test audit doc against drift.

The audit lives at ``Docs/superpowers/specs/2026-06-23-r33-skip-tests-audit.md``.
These tests assert the doc exists, parses as markdown, and mentions every
expected skip category so a future edit (or a newly-added skip that nobody
catalogued) is caught at review time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = REPO_ROOT / "Docs" / "superpowers" / "specs" / "2026-06-23-r33-skip-tests-audit.md"

# Every skip category keyword the audit must mention. Each maps to a real
# skip found in tests/ during the R33-E grep pass.
EXPECTED_KEYWORDS = {
    "Postgres": "TEST_POSTGRES_URL live-PG skips (db_abstraction, dual-backend stores, migration_compat, storage_backend)",
    "Kafka": "kafka references (note: stubbed, no live-broker skip — audit must state this)",
    "Windows": "Windows WAL skip in test_rca_tool_call_log.py",
    "helm": "helm CLI not-installed skips (test_helm_templates, test_r29_pg_defaulting, test_local_ci)",
    "ruff": "ruff not-installed skips in test_local_ci.py",
    "bandit": "bandit not-installed skip in test_local_ci.py",
    "npm": "npm not-installed skip (frontend vitest) in test_local_ci.py",
    "OCR": "rapidocr/tesseract availability skips in test_ocr_parser.py",
}

# Specific source files that contain skips — the audit must reference each.
EXPECTED_FILES = [
    "test_db_abstraction.py",
    "test_helm_templates.py",
    "test_knowledge_store_dual_backend.py",
    "test_local_ci.py",
    "test_migration_postgres_compat.py",
    "test_ocr_parser.py",
    "test_rca_tool_call_log.py",
    "test_storage_backend.py",
    "test_platform_run_store_dual_backend.py",
    "test_rca_platform_dual_backend.py",
    "test_r29_pg_defaulting.py",
    "test_r33a_checkpointer_factory.py",
    "test_path_guard.py",
]


@pytest.fixture(scope="module")
def audit_text() -> str:
    if not AUDIT_PATH.is_file():
        pytest.fail(f"skip-test audit doc missing: {AUDIT_PATH}")
    return AUDIT_PATH.read_text(encoding="utf-8")


def test_audit_doc_exists() -> None:
    assert AUDIT_PATH.is_file(), f"audit doc not found at {AUDIT_PATH}"


def test_audit_doc_parses_as_markdown(audit_text: str) -> None:
    # A markdown doc has at least one heading line starting with '#'.
    headings = [ln for ln in audit_text.splitlines() if ln.startswith("#")]
    assert headings, "audit doc has no markdown headings"
    # And at least one table row (the catalog is table-driven).
    assert "|" in audit_text, "audit doc has no markdown table"


def test_audit_doc_mentions_every_category_keyword(audit_text: str) -> None:
    missing = [kw for kw in EXPECTED_KEYWORDS if kw not in audit_text]
    assert not missing, f"audit doc missing category keywords: {missing}"


def test_audit_doc_references_every_skipped_source_file(audit_text: str) -> None:
    missing = [f for f in EXPECTED_FILES if f not in audit_text]
    assert not missing, f"audit doc does not reference skipped source files: {missing}"


def test_audit_doc_states_legitimacy_per_entry(audit_text: str) -> None:
    # Each catalogued skip must be marked legitimate (or flagged in Concerns).
    # We require the word "legitimate" to appear, plus a Concerns section.
    assert "legitimate" in audit_text.lower(), "audit doc must state legitimacy per skip entry"
    assert "Concerns" in audit_text, "audit doc must have a Concerns section"


def test_audit_doc_covers_skipif_and_importorskip(audit_text: str) -> None:
    # The grep found both @pytest.mark.skipif and pytest.importorskip usages.
    assert "skipif" in audit_text, "audit doc must cover @pytest.mark.skipif occurrences"
    assert "importorskip" in audit_text, "audit doc must cover pytest.importorskip occurrences"


def test_audit_doc_has_unskips_when_column(audit_text: str) -> None:
    # CI operators need to know which env un-skips each entry.
    assert "un-skip" in audit_text.lower() or "unskips" in audit_text.lower(), (
        "audit doc must document the env that un-skips each entry"
    )
