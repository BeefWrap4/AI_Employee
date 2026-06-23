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
WEB_NGINX_PATH = ROOT / "apps" / "web-portal" / "nginx.conf"
WEB_DOCKERIGNORE_PATH = ROOT / "apps" / "web-portal" / ".dockerignore"
PY_DEPS_SCRIPT_PATH = ROOT / "scripts" / "docker" / "requirements_from_pyproject.py"

CANONICAL_COMPOSE_APP_SERVICES = {
    "web-portal",
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


def test_web_portal_nginx_proxies_to_compose_service_names() -> None:
    text = WEB_NGINX_PATH.read_text(encoding="utf-8")
    assert ".svc.cluster.local" not in text
    for upstream in (
        "http://knowledge-api:8010/",
        "http://rca-agent:8020/",
        "http://agent-platform-api:8030/",
        "http://tool-registry:8040/",
    ):
        assert upstream in text


def test_web_portal_compose_build_context_is_frontend_only() -> None:
    web = _compose()["services"]["web-portal"]
    build = web["build"]
    assert build["context"].replace("\\", "/") == "../../apps/web-portal"
    assert build["dockerfile"] == "Dockerfile"


def test_web_portal_has_local_dockerignore_for_generated_assets() -> None:
    entries = set(WEB_DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines())
    assert "node_modules/" in entries
    assert "dist/" in entries


def test_compose_app_data_mounts_use_local_bind_paths() -> None:
    services = _compose()["services"]
    for service_name in (
        "knowledge-api",
        "ingestion-worker",
        "rca-agent",
        "agent-platform-api",
        "tool-registry",
        "approval-service",
    ):
        volumes = services[service_name].get("volumes", [])
        assert any(str(v).startswith("../../var/docker/") for v in volumes), service_name


def test_compose_bind_mounted_app_services_can_write_local_data_dirs() -> None:
    services = _compose()["services"]
    for service_name in (
        "knowledge-api",
        "ingestion-worker",
        "rca-agent",
        "agent-platform-api",
        "tool-registry",
        "approval-service",
    ):
        assert services[service_name].get("user") == "0", service_name


def test_approval_service_uses_postgres_in_compose() -> None:
    env = _compose()["services"]["approval-service"]["environment"]
    assert env["DATABASE_URL"].startswith("postgresql://")


def test_python_service_dockerfiles_cache_dependency_install_before_source_copy() -> None:
    assert PY_DEPS_SCRIPT_PATH.exists()
    for dockerfile in (ROOT / "services").glob("*/Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        if "pip install -e" not in text:
            continue
        assert "requirements_from_pyproject.py" in text, dockerfile
        assert "pip install -r /tmp/requirements.txt" in text, dockerfile
        assert "pip install -e . --no-deps" in text, dockerfile
        assert 'pip install -e ".[dev]" PyJWT' not in text, dockerfile
        deps_install = text.index("pip install -r /tmp/requirements.txt")
        services_copy = text.index("COPY services/")
        packages_copy = text.index("COPY packages/")
        editable_install = text.index("pip install -e . --no-deps")
        assert deps_install < services_copy < editable_install, dockerfile
        assert deps_install < packages_copy < editable_install, dockerfile


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
