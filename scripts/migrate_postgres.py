#!/usr/bin/env python
"""Run Alembic migrations against the configured database (R16-5).

Usage::

    # Postgres (prod) — DATABASE_URL drives the target:
    DATABASE_URL=postgres://ai_employee:ai_employee@localhost:5432/ai_employee \
        python scripts/migrate_postgres.py upgrade

    # Or pin the migration DB explicitly:
    MIGRATION_DATABASE_URL=postgres://... python scripts/migrate_postgres.py upgrade

    # SQLite (dev/test):
    python scripts/migrate_postgres.py upgrade

Supports ``upgrade`` (to head), ``downgrade`` (to base), and ``current``.
The baseline migration is dialect-aware (R16-2): it runs on both SQLite
and PostgreSQL.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_db_url() -> str:
    """Pick the migration target URL.

    Order: MIGRATION_DATABASE_URL > DATABASE_URL > alembic.ini default (SQLite).
    """
    url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    return url


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    url = _resolve_db_url()
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    action = argv[1].lower()
    try:
        from alembic import command
    except ImportError:
        print("alembic not installed; run: pip install alembic", file=sys.stderr)
        return 2

    cfg = _alembic_config()
    url = cfg.get_main_option("sqlalchemy.url") or "(alembic.ini default)"
    print(f"[migrate] target url: {url}")

    if action == "upgrade":
        command.upgrade(cfg, "head")
        print("[migrate] upgrade -> head OK")
    elif action == "downgrade":
        command.downgrade(cfg, "base")
        print("[migrate] downgrade -> base OK")
    elif action == "current":
        command.current(cfg)
    else:
        print(f"unknown action: {action!r} (use upgrade|downgrade|current)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
