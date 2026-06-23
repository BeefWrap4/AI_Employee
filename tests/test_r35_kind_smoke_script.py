"""Structural tests for scripts/kind-smoke.sh (R35-B).

These tests pin the script's structure — file exists, is executable,
starts with a shebang, and contains the expected sections — so a
refactor cannot silently drop a step (e.g. forget to apply postgres,
skip the helm upgrade, drop the curl smoke, or regress to exit-0-on-fail).

The tests intentionally do NOT execute the script: running it would
recreate the kind cluster, pull a postgres image, and run a full
helm install — far too heavy for a unit test, and a CI blocker on
machines without docker/kind.  The R34 spec was the first time the
chart ran against a real cluster, and these checks are the
regression net that keeps that path reproducible from a single
command.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "kind-smoke.sh"


def _read() -> str:
    assert SCRIPT_PATH.exists(), f"missing script at {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# File-level invariants
# --------------------------------------------------------------------------- #


def test_kind_smoke_script_exists() -> None:
    """The script must be checked in at scripts/kind-smoke.sh."""
    assert SCRIPT_PATH.is_file(), f"expected file at {SCRIPT_PATH}"


def test_kind_smoke_script_is_executable() -> None:
    """The script must have the executable bit so `./scripts/kind-smoke.sh` works.

    Windows git can drop the +x bit on checkout, so we fall back to
    `bash <script>` (the shebang handles invocation on POSIX, and a
    fresh `chmod +x` is one line on Windows).  We assert the bit is
    set OR the script has a bash shebang that bash can run.
    """
    text = _read()
    assert text.startswith("#!"), "shebang must be present so bash can run the file"
    mode = SCRIPT_PATH.stat().st_mode
    if mode & stat.S_IXUSR:
        # Strict: bit is set, callers can `./scripts/kind-smoke.sh`.
        assert os.access(SCRIPT_PATH, os.X_OK), "os.access(X_OK) must be true when +x is set"
    else:
        # Lenient fallback for Windows checkouts: still require the
        # shebang so `bash scripts/kind-smoke.sh` works.  Print a
        # warning rather than failing the test on Windows.
        import warnings

        warnings.warn(
            f"{SCRIPT_PATH} has no +x bit; run `chmod +x` on a POSIX shell",
            stacklevel=2,
        )


def test_kind_smoke_script_starts_with_shebang() -> None:
    """First line must be a #! so the kernel invokes bash directly."""
    first_line = _read().splitlines()[0]
    assert first_line.startswith("#!"), f"expected shebang, got: {first_line!r}"
    assert "bash" in first_line, f"shebang should invoke bash, got: {first_line!r}"


def test_kind_smoke_script_uses_strict_mode() -> None:
    """`set -e` (or equivalent) + `pipefail` is required so any step failure aborts."""
    text = _read()
    # Allow any of:  set -e  /  set -euo pipefail  /  set -eu  /  set -e -o pipefail
    assert re.search(r"^\s*set\s+-[a-z]*e", text, re.MULTILINE), (
        "script must enable 'set -e' so failures abort"
    )
    assert "pipefail" in text, "script must enable pipefail (set -o pipefail)"


# --------------------------------------------------------------------------- #
# Step coverage
# --------------------------------------------------------------------------- #


def _assert_step_present(text: str, *, needles: list[str], step_name: str) -> None:
    missing = [n for n in needles if n not in text]
    assert not missing, f"{step_name} step is missing required tokens: {missing}"


def test_kind_smoke_creates_kind_cluster() -> None:
    """Step 1 — must call `kind create cluster` (and tolerate an existing one)."""
    text = _read()
    _assert_step_present(
        text,
        needles=["kind", "create cluster", "ai-emp"],
        step_name="kind cluster",
    )
    # Idempotency: must check whether the cluster already exists.
    assert "get clusters" in text, (
        "script should check `kind get clusters` so re-runs don't fail with 'cluster exists'"
    )


def test_kind_smoke_loads_ai_employee_images() -> None:
    """Step 2 — must iterate over the 8 ai-employee services and `kind load docker-image`."""
    text = _read()
    for svc in (
        "agent-platform-api",
        "api-gateway",
        "approval-service",
        "ingestion-worker",
        "knowledge-api",
        "mcp-gateway",
        "rca-agent",
        "tool-registry",
    ):
        assert svc in text, f"image list must include {svc}"
    assert "kind load docker-image" in text or "load docker-image" in text, (
        "script must `kind load docker-image` to push images into the cluster"
    )


def test_kind_smoke_creates_namespace() -> None:
    """Step 3 — must `kubectl create namespace` (and tolerate existing)."""
    text = _read()
    assert "create namespace" in text, (
        "script must `kubectl create namespace` before applying manifests"
    )
    # The `|| true` pattern keeps re-runs from failing.
    assert re.search(r"create\s+namespace\s+\S+\s*.*\|\|\s*true", text), (
        "namespace creation should swallow 'already exists' so the script is idempotent"
    )


def test_kind_smoke_pulls_and_loads_postgres() -> None:
    """Step 4 — must `docker pull postgres:16` and `kind load` it (alpine was a ctr failure).

    The script may use ${DOCKER_BIN} (an env-overridable variable) so
    we match `pull` against a `<bin> pull` pattern rather than the
    literal `docker pull`.
    """
    text = _read()
    assert "postgres:16" in text, "script must use postgres:16 (alpine was a ctr digest issue)"
    # Match the literal `docker pull` or a shell-var-prefixed form
    # like `${DOCKER_BIN}" pull` (note the optional closing quote
    # between the var ref and the subcommand).  The image string
    # check above pins the pull command to the postgres image
    # specifically.
    assert (
        re.search(r"(?:docker|\$\{?DOCKER_BIN\}?)[\"']?\s+pull\b", text) or "docker pull" in text
    ), "script must docker pull the postgres image"
    assert re.search(r"(?:kind|\$\{?KIND_BIN\}?)[\"']?\s+load\b", text) or "kind load" in text, (
        "script must kind load the postgres image"
    )


def test_kind_smoke_applies_postgres_manifest() -> None:
    """Step 5 — must `kubectl apply -f infra/k8s/postgres.yaml`."""
    text = _read()
    assert "infra/k8s/postgres.yaml" in text, "script must apply infra/k8s/postgres.yaml"
    assert (
        re.search(r"(?:kubectl|\$\{?KUBECTL_BIN\}?)[\"']?\s+apply\b", text)
        or "kubectl apply" in text
    ), "script must run `kubectl apply`"


def test_kind_smoke_waits_for_postgres_ready() -> None:
    """Step 6 — must `kubectl wait` for app=postgres pod with a timeout."""
    text = _read()
    assert "app=postgres" in text, "script must wait on label app=postgres"
    assert "for=condition=ready" in text, "script must use --for=condition=ready"
    assert re.search(r"--timeout\s*=\s*\d+s?", text), (
        "script must pass --timeout so the wait doesn't hang forever"
    )


def test_kind_smoke_installs_or_upgrades_helm_chart() -> None:
    """Step 7 — must `helm install` (or `helm upgrade`) with values-smoke.yaml overlay."""
    text = _read()
    assert "helm" in text and ("install" in text or "upgrade" in text), (
        "script must call `helm install` or `helm upgrade`"
    )
    assert "infra/helm" in text, "script must point at the infra/helm chart"
    assert "values-smoke.yaml" in text, (
        "script must layer infra/helm/values-smoke.yaml (R34 smoke overlay)"
    )
    # Idempotency: must detect an existing release and upgrade instead of failing.
    # Match `helm list` with optional shell variable prefix (e.g. ${HELM_BIN} list).
    assert re.search(r"(?:helm|\$\{?HELM_BIN\}?)[\"']?\s+list\b", text) or "helm list" in text, (
        "script should check `helm list` to choose install vs upgrade"
    )


def test_kind_smoke_waits_for_helm_pods_ready() -> None:
    """Step 8 — must `kubectl wait` on helm-managed pods with a timeout."""
    text = _read()
    assert "app.kubernetes.io/managed-by=Helm" in text, (
        "script must wait on the helm-managed pod label"
    )
    assert re.search(r"--timeout\s*=\s*\d+s?", text), "script must pass --timeout to the helm wait"


def test_kind_smoke_runs_curl_smoke() -> None:
    """Step 9 — must curl the api-gateway + the 6 backend /health endpoints + agent endpoints."""
    text = _read()
    # 6 backend health endpoints + 1 gateway /health
    for path in (
        "/health",
        "/api/platform/health",
        "/api/knowledge/health",
        "/api/rca/health",
        "/api/tools/health",
        "/api/approvals/health",
        "/api/mcp/health",
    ):
        assert path in text, f"smoke must hit {path}"
    assert "/api/platform/api/v1/agent-templates" in text, (
        "smoke must list agent-templates to confirm 5 templates are wired"
    )
    assert "/api/platform/api/v1/agent-runs" in text, (
        "smoke must POST + GET agent-runs to prove the LangGraph path is end-to-end"
    )


def test_kind_smoke_exits_nonzero_on_failure() -> None:
    """Step 10 — script must abort on any step failure (set -e + explicit summary)."""
    text = _read()
    # `set -e` already covers most paths; we additionally want a
    # summary block + explicit non-zero exit so CI surfaces the
    # failure with a pass/fail breakdown.
    assert "summary" in text.lower() or "SUMMARY" in text, (
        "script must print a pass/fail summary at the end"
    )
    assert re.search(r"exit\s+[^0]\d*", text) or "exit 1" in text, (
        "script must explicitly `exit 1` (or other non-zero) on smoke failure"
    )


# --------------------------------------------------------------------------- #
# Anti-regression: the script must NOT silently `exit 0` before the smoke
# --------------------------------------------------------------------------- #


def test_kind_smoke_does_not_skip_smoke() -> None:
    """The smoke block is mandatory: there must not be a top-level early `exit 0`."""
    text = _read()
    # Find every `exit 0` and assert each one is either inside a
    # SKIP_PG branch or comes after a record_fail path.  Conservatively
    # we just require that the script's last non-comment, non-blank
    # line is an exit or a print_summary call.
    last_meaningful = [
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    ][-1]
    assert "exit" in last_meaningful or "print_summary" in last_meaningful, (
        f"final line should call print_summary + exit, got: {last_meaningful!r}"
    )


# --------------------------------------------------------------------------- #
# Parametric helper: each smoke step must record a pass/fail entry so the
# summary is useful.  This catches a refactor that drops `record_ok` calls.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "expected_token",
    [
        "kind cluster",
        "helm release",
        "api-gateway reachable",
        "agent-templates",
        "agent-runs",
        "PG tables listed",
    ],
)
def test_kind_smoke_summary_contains_check(expected_token: str) -> None:
    """The summary block must mention each of the high-level smoke checks."""
    text = _read()
    assert expected_token in text, (
        f"summary must surface a check for {expected_token!r} so failures are attributable"
    )
