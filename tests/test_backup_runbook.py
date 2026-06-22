"""R30-C: backup runbook contract tests.

Pins the structural contract documented in ``docs/backup-runbook.md`` so that
accidental edits to ``scripts/backup.sh`` or the Helm CronJob fail loud in CI
rather than only at 02:00 UTC on the production cluster.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "backup.sh"
CRONJOB_PATH = REPO_ROOT / "infra" / "helm" / "templates" / "backup-cronjob.yaml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "backup-runbook.md"


# --------------------------------------------------------------------------- #
# 1. Backup script — shell hygiene + required primitives
# --------------------------------------------------------------------------- #


def test_backup_script_exists() -> None:
    """``scripts/backup.sh`` must exist on disk."""
    assert SCRIPT_PATH.is_file(), f"missing {SCRIPT_PATH}"


def test_backup_script_has_shebang() -> None:
    """The script must start with a bash shebang so cron/k8s can exec it.

    We don't gate on the x bit (Windows doesn't honour Unix mode bits) —
    the shebang plus the documented k8s CronJob invocation is enough.
    """
    first_line = SCRIPT_PATH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), "backup.sh must start with a shebang"
    assert "bash" in first_line, f"backup.sh shebang must invoke bash (got {first_line!r})"


def test_backup_script_uses_strict_mode() -> None:
    """The script must enable ``set -euo pipefail`` early in the file."""
    lines = SCRIPT_PATH.read_text(encoding="utf-8").splitlines()
    # Look for strict-mode anywhere in the first 30 lines (after the shebang
    # and the docstring comment).
    head = "\n".join(lines[:30])
    assert "set -euo pipefail" in head, (
        "scripts/backup.sh must enable strict mode (set -euo pipefail) within the first 30 lines"
    )


def test_backup_script_runs_pg_dump_with_custom_format() -> None:
    """Postgres dump must use ``--format=custom`` so ``pg_restore`` works."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "pg_dump" in text, "scripts/backup.sh must call pg_dump"
    assert "--format=custom" in text, (
        "pg_dump invocation must use --format=custom (compressed, pg_restore-compatible)"
    )


def test_backup_script_runs_mc_mirror_to_a_different_alias() -> None:
    """MinIO mirror must use ``mc mirror`` and target a *secondary* alias."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "mc mirror" in text, "MinIO backup must use `mc mirror`"
    # The destination alias must not be the primary one.
    assert re.search(r"mc mirror[^|]*secondary", text, re.MULTILINE), (
        "MinIO mirror must target a *secondary* alias (offsite copy)"
    )


def test_backup_script_uses_redis_bgsave() -> None:
    """Redis step must use the non-blocking ``BGSAVE`` command."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert re.search(r"redis-cli[^\n]*\bBGSAVE\b", text), (
        "Redis backup must use the non-blocking BGSAVE command (not SAVE)"
    )


def test_backup_script_writes_a_manifest() -> None:
    """A MANIFEST.json must be written for the on-call to triage the run."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "MANIFEST.json" in text, (
        "scripts/backup.sh must emit a MANIFEST.json with sizes/checksums"
    )


def test_backup_script_supports_subsets_by_name() -> None:
    """The script must accept subset names (``pg``, ``minio``, ``redis``) as args."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    for needle in ('"pg"', '"minio"', '"redis"'):
        assert needle in text, (
            f"backup.sh must recognise subset selector {needle} as a CLI argument"
        )


# --------------------------------------------------------------------------- #
# 2. Helm CronJob — schedule + required wiring
# --------------------------------------------------------------------------- #


def test_backup_cronjob_yaml_exists() -> None:
    assert CRONJOB_PATH.is_file(), f"missing {CRONJOB_PATH}"


def _load_cronjob() -> dict:
    """Load the CronJob manifest, supporting multi-doc YAML."""
    docs = list(yaml.safe_load_all(CRONJOB_PATH.read_text(encoding="utf-8")))
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "CronJob":
            return doc
    raise AssertionError(f"no CronJob document found in {CRONJOB_PATH}")


def test_backup_cronjob_runs_daily() -> None:
    """The CronJob must run at most once per day (RPO target = 24 h for MinIO)."""
    data = _load_cronjob()
    assert data["kind"] == "CronJob"
    schedule = data["spec"]["schedule"]
    # Five-field cron: minute hour dom month dow. We only care about
    # the first two (rejected step expressions); the others are free.
    fields = schedule.split()
    minute, hour = fields[0], fields[1]
    assert minute.isdigit() and hour.isdigit(), (
        f"CronJob schedule {schedule!r} must pin a specific minute+hour"
    )
    # Reject "every minute/hour" forms. minute and hour must be pinned to
    # specific values (already checked above), so a fire-every-N-min schedule
    # would have been rejected. Also ensure the schedule is bound to a
    # specific calendar (e.g. dom=* is fine; dow=* and month=* and dom=*
    # together means "every day", which is what we want).
    # The real anti-pattern is e.g. */5 in minute.
    assert "/" not in minute and "/" not in hour, (
        f"CronJob schedule {schedule!r} uses step expression (*/N); must be fixed time"
    )
    # Reject the classic "*/5 * * * *" (every 5 min) pattern.
    assert "*" not in minute and "*" not in hour, (
        f"CronJob schedule {schedule!r} would fire too often (per-minute or per-hour)"
    )


def test_backup_cronjob_invokes_backup_script() -> None:
    """The CronJob container must exec the script we ship."""
    data = _load_cronjob()
    containers = data["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    cmds = [c.get("command", []) for c in containers]
    flat = [str(part) for sublist in cmds for part in sublist]
    assert any("backup.sh" in s for s in flat), "CronJob container must execute scripts/backup.sh"


def test_backup_cronjob_has_pvc_mount() -> None:
    """The CronJob must mount a PVC so the artefact survives pod restart."""
    data = _load_cronjob()
    containers = data["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    mounts = []
    for c in containers:
        mounts.extend(c.get("volumeMounts", []))
    pvc_mounts = [m for m in mounts if m.get("name")]
    assert pvc_mounts, "CronJob must mount at least one volume for artefact persistence"
    # The corresponding volume should be a PVC.
    volumes = data["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("volumes", [])
    pvc_volumes = [v for v in volumes if "persistentVolumeClaim" in v]
    assert pvc_volumes, "CronJob must declare a persistentVolumeClaim-backed volume"


# --------------------------------------------------------------------------- #
# 3. Runbook doc — keeps the prose in sync with the code
# --------------------------------------------------------------------------- #


def test_runbook_doc_exists_and_mentions_all_three_subsystems() -> None:
    """The runbook must exist and reference PG / MinIO / Redis by name."""
    assert RUNBOOK_PATH.is_file(), f"missing {RUNBOOK_PATH}"
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    for needle in ("PostgreSQL", "MinIO", "Redis"):
        assert needle in text, f"runbook must document the {needle} backup flow"
    assert "RPO" in text and "RTO" in text, "runbook must define RPO/RTO targets"


def test_runbook_references_the_script_and_cronjob() -> None:
    """The runbook must cross-link the actual script and Helm manifest paths."""
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "scripts/backup.sh" in text
    assert "infra/helm/templates/backup-cronjob.yaml" in text
