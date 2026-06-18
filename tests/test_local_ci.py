"""Local CI smoke test.

Runs the same checks the GitHub Actions CI runs, so we can catch
issues before pushing.  Failures here print which command failed.

Usage:
    python -m pytest tests/test_local_ci.py -v
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 300) -> tuple[int, str]:
    """Run a shell command, return (exit_code, output)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd or REPO_ROOT,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


# --------------------------------------------------------------------------- #
# pytest
# --------------------------------------------------------------------------- #


def test_pytest_passes() -> None:
    code, out = _run(["python", "-m", "pytest", "-q", "--tb=short"])
    assert code == 0, f"pytest failed:\n{out[-2000:]}"


# --------------------------------------------------------------------------- #
# ruff (lint + format check)
# --------------------------------------------------------------------------- #


def test_ruff_lint_passes() -> None:
    if shutil.which("ruff") is None:
        pytest.skip("ruff not installed locally")
    code, out = _run(["ruff", "check", "packages", "services", "tests"])
    # CI gate: hard fail on any ruff error.  Local dev may tolerate
    # pending cleanups — if this test is the only thing failing, the
    # team should run ``ruff check --fix`` to bring the repo current.
    assert code == 0, (
        f"ruff lint failed with {code}:\n{out[-2000:]}\n"
        "Hint: run `ruff check --fix packages services tests` and commit the result."
    )


def test_ruff_format_check_passes() -> None:
    if shutil.which("ruff") is None:
        pytest.skip("ruff not installed locally")
    code, out = _run(["ruff", "format", "--check", "packages", "services", "tests"])
    # Exit code 1 is "would reformat", 0 is "clean".
    assert code == 0, f"ruff format would reformat:\n{out[-2000:]}"


# --------------------------------------------------------------------------- #
# bandit (security)
# --------------------------------------------------------------------------- #


def test_bandit_passes() -> None:
    if shutil.which("bandit") is None:
        pytest.skip("bandit not installed locally")
    code, out = _run(["bandit", "-r", "packages", "services", "-q", "--exclude", "tests"])
    assert code == 0, f"bandit failed:\n{out[-2000:]}"


# --------------------------------------------------------------------------- #
# frontend vitest
# --------------------------------------------------------------------------- #


def test_frontend_vitest_passes() -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm not installed locally")
    code, out = _run(
        ["npx", "vitest", "run"],
        cwd=REPO_ROOT / "apps" / "web-portal",
    )
    assert code == 0, f"vitest failed:\n{out[-2000:]}"


# --------------------------------------------------------------------------- #
# helm template
# --------------------------------------------------------------------------- #


def test_helm_template_renders() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm not installed locally")
    code, out = _run(["helm", "template", "ai-employee", "infra/helm"])
    assert code == 0, f"helm template failed:\n{out[-2000:]}"
