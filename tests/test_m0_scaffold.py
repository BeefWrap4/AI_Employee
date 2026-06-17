from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_m0_repository_scaffold_exists() -> None:
    expected_paths = [
        "apps/web-portal",
        "services/knowledge-api",
        "services/ingestion-worker",
        "services/rca-agent",
        "services/agent-platform-api",
        "services/tool-registry",
        "services/eval-service",
        "packages/common-schemas",
        "packages/llm-gateway",
        "packages/auth-policy",
        "packages/observability",
        "infra/docker-compose/compose.yml",
        "infra/k8s",
        "infra/helm",
        "scripts",
        "tests/rag-eval",
        "tests/rca-replay",
        "tests/platform-e2e",
    ]

    missing = [path for path in expected_paths if not (ROOT / path).exists()]

    assert missing == []


def test_m0_root_tooling_files_exist() -> None:
    expected_files = [
        "pyproject.toml",
        "pytest.ini",
        ".github/workflows/ci.yml",
    ]

    missing = [path for path in expected_files if not (ROOT / path).is_file()]

    assert missing == []
