from __future__ import annotations

import configparser
import importlib.util
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

yaml = pytest.importorskip("yaml", reason="PyYAML required for repo config checks")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "infra" / "docker-compose" / "compose.yml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
PYTEST_INI_PATH = ROOT / "pytest.ini"
LOCAL_CI_PATH = ROOT / "tests" / "test_local_ci.py"

CANONICAL_COMPOSE_APP_SERVICES = {
    "knowledge-api",
    "ingestion-worker",
    "rca-agent",
    "agent-platform-api",
    "tool-registry",
    "approval-service",
    "mcp-gateway",
    "event-gateway",
    "api-gateway",
}


def _compose() -> dict:
    with COMPOSE_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _depends_on_names(depends_on: object) -> set[str]:
    if depends_on is None:
        return set()
    if isinstance(depends_on, list):
        return set(depends_on)
    if isinstance(depends_on, dict):
        return set(depends_on)
    raise AssertionError(f"unsupported depends_on shape: {depends_on!r}")


def test_compose_declares_canonical_application_services() -> None:
    services = _compose()["services"]
    missing = sorted(CANONICAL_COMPOSE_APP_SERVICES - set(services))
    assert missing == []


def test_compose_depends_on_references_declared_services() -> None:
    services = _compose()["services"]
    undefined: dict[str, list[str]] = {}
    for name, config in services.items():
        missing = sorted(_depends_on_names(config.get("depends_on")) - set(services))
        if missing:
            undefined[name] = missing
    assert undefined == {}


def _pyproject_source_roots() -> list[str]:
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    package_dirs = data["tool"]["setuptools"]["package-dir"].values()
    roots = []
    for package_dir in package_dirs:
        prefix, _, _ = package_dir.partition("/ai_employee/")
        roots.append(prefix)
    return list(dict.fromkeys(roots))


def _pytest_ini_pythonpath() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI_PATH, encoding="utf-8")
    raw = parser["pytest"].get("pythonpath", "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _pyproject_pytest_pythonpath() -> list[str]:
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return data["tool"]["pytest"]["ini_options"]["pythonpath"]


def test_pytest_ini_pythonpath_matches_packaged_source_roots() -> None:
    assert _pytest_ini_pythonpath() == _pyproject_source_roots()


def test_pyproject_pytest_pythonpath_matches_packaged_source_roots() -> None:
    assert _pyproject_pytest_pythonpath() == _pyproject_source_roots()


def test_pytest_markers_do_not_contain_source_paths() -> None:
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI_PATH, encoding="utf-8")
    raw = parser["pytest"].get("markers", "")
    path_like = [line.strip() for line in raw.splitlines() if "/" in line.strip()]
    assert path_like == []


def test_local_ci_pytest_command_ignores_local_ci_to_avoid_recursion() -> None:
    spec = importlib.util.spec_from_file_location("local_ci_contract", LOCAL_CI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cmd = getattr(module, "FULL_PYTEST_CMD", [])
    assert "tests" in cmd
    assert "--ignore=tests/test_local_ci.py" in cmd
