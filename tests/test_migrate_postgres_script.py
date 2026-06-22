"""Smoke test for the Postgres migration runner script (R16-5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

alembic = pytest.importorskip("alembic", reason="alembic required")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_postgres.py"


def test_script_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Calling main with no action prints help and returns 0."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("migrate_postgres_help", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["migrate_postgres"]) == 0
    out = capsys.readouterr().out
    assert "upgrade" in out and "downgrade" in out


def test_script_main_upgrade_against_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script's main() applies the baseline migration to a fresh SQLite DB."""
    db_url = f"sqlite:///{tmp_path}/mig.sqlite3"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", db_url)

    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("migrate_postgres", SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.main(["migrate_postgres", "upgrade"]) == 0

        # Verify tables exist on the migrated DB.
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "mig.sqlite3"))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic%'"
        ).fetchall()
        conn.close()
        tables = {r[0] for r in rows}
        assert {"documents", "chunks", "agent_runs", "tools"}.issubset(tables)
    finally:
        sys.path.pop(0)


def test_script_main_current_reports_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = f"sqlite:///{tmp_path}/mig.sqlite3"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", db_url)

    import importlib.util

    spec = importlib.util.spec_from_file_location("migrate_postgres2", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["migrate_postgres", "upgrade"]) == 0
    # `current` should not error after upgrade.
    assert mod.main(["migrate_postgres", "current"]) == 0


def test_script_unknown_action_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/mig.sqlite3")
    import importlib.util

    spec = importlib.util.spec_from_file_location("migrate_postgres3", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["migrate_postgres", "frobnicate"]) == 2


def test_resolve_db_url_prefers_migration_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("migrate_postgres4", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgres://a:b@c:5432/d")
    monkeypatch.setenv("DATABASE_URL", "postgres://x:y@z:5432/w")
    assert mod._resolve_db_url() == "postgres://a:b@c:5432/d"
