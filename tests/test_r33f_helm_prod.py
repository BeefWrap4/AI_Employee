"""R33-F: production Helm values overlay.

``infra/helm/values-prod.yaml`` is an overlay meant to be merged on top
of the dev ``values.yaml`` via ``helm install ... -f values.yaml -f
values-prod.yaml``.  These tests load both files, perform a Helm-style
recursive merge (prod wins on scalars; nested maps are merged
recursively; lists are replaced wholesale, matching Helm's ``coalesce``
behaviour), and assert the production flips the operator expects:

* ``knowledge-api`` runs >=2 replicas (was 1 in dev).
* every enabled service carries a ``resources.limits`` block.
* ``rca-agent``, ``api-gateway`` and ``mcp-gateway`` gain an HPA in
  addition to ``agent-platform-api``.
* ``API_GATEWAY_AUTH_REQUIRED`` and ``RATE_LIMIT_ENABLED`` flip to
  ``"true"`` (the code default stays ``"false"`` for dev).
* ``ingress.enabled`` is true (with TLS for production).

The overlay itself must NOT change the code default of AUTH / RATE_LIMIT
— production enforcement is opt-in via the overlay only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required")

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "infra" / "helm"
VALUES_PATH = CHART_PATH / "values.yaml"
PROD_VALUES_PATH = CHART_PATH / "values-prod.yaml"
README_PATH = CHART_PATH / "README.md"


def _deep_merge(base: Any, override: Any) -> Any:
    """Helm-style coalesce: override wins on scalars; nested maps merge
    recursively; lists are replaced wholesale."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged: dict[str, Any] = dict(base)
        for key, value in override.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    # Scalars or lists: override wins outright.
    return override


@pytest.fixture(scope="module")
def dev_values() -> dict[str, Any]:
    with VALUES_PATH.open() as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def prod_values() -> dict[str, Any]:
    if not PROD_VALUES_PATH.exists():
        pytest.fail(f"missing production overlay: {PROD_VALUES_PATH}")
    with PROD_VALUES_PATH.open() as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def merged_values(dev_values: dict[str, Any], prod_values: dict[str, Any]) -> dict[str, Any]:
    return _deep_merge(dev_values, prod_values)


# --------------------------------------------------------------------------- #
# Cycle 1: production overlay flips
# --------------------------------------------------------------------------- #


def test_prod_overlay_file_exists() -> None:
    assert PROD_VALUES_PATH.exists(), "values-prod.yaml overlay must exist"


def test_knowledge_api_replicas_raised_in_prod(merged_values: dict[str, Any]) -> None:
    """dev pins knowledge-api to 1 (SQLite); prod raises it to >=2."""
    replicas = merged_values["services"]["knowledge-api"]["replicas"]
    assert replicas >= 2, f"knowledge-api replicas {replicas} < 2 in prod"


def test_every_service_has_resource_limits(merged_values: dict[str, Any]) -> None:
    """Every enabled service carries a resources.limits block in prod."""
    services = merged_values["services"]
    missing: list[str] = []
    for name, svc in services.items():
        if not svc.get("enabled", True):
            continue
        limits = svc.get("resources", {}).get("limits")
        if not limits:
            missing.append(name)
    assert not missing, f"services missing resources.limits in prod: {missing}"


def test_every_service_has_resource_requests(merged_values: dict[str, Any]) -> None:
    """Every enabled service carries a resources.requests block in prod."""
    services = merged_values["services"]
    missing: list[str] = []
    for name, svc in services.items():
        if not svc.get("enabled", True):
            continue
        requests = svc.get("resources", {}).get("requests")
        if not requests:
            missing.append(name)
    assert not missing, f"services missing resources.requests in prod: {missing}"


def test_agent_platform_scaled_higher_than_baseline(merged_values: dict[str, Any]) -> None:
    """agent-platform-api gets a larger limit than the baseline 500m/512Mi."""
    limits = merged_values["services"]["agent-platform-api"]["resources"]["limits"]
    # Baseline overlay is cpu 500m / memory 512Mi; the platform is the
    # busiest service so it should be scaled above that.
    assert limits["cpu"] not in (None, "500m")
    assert limits["memory"] not in (None, "512Mi")


def test_hpa_enabled_for_rca_agent(merged_values: dict[str, Any]) -> None:
    hpa = merged_values["services"]["rca-agent"].get("hpa", {})
    assert hpa.get("enabled") is True, "rca-agent must have HPA in prod"


def test_hpa_enabled_for_api_gateway(merged_values: dict[str, Any]) -> None:
    hpa = merged_values["services"]["api-gateway"].get("hpa", {})
    assert hpa.get("enabled") is True, "api-gateway must have HPA in prod"


def test_hpa_enabled_for_mcp_gateway(merged_values: dict[str, Any]) -> None:
    hpa = merged_values["services"]["mcp-gateway"].get("hpa", {})
    assert hpa.get("enabled") is True, "mcp-gateway must have HPA in prod"


def test_hpa_still_enabled_for_agent_platform(merged_values: dict[str, Any]) -> None:
    """The dev HPA on agent-platform-api must survive the merge."""
    hpa = merged_values["services"]["agent-platform-api"].get("hpa", {})
    assert hpa.get("enabled") is True


def test_auth_required_flipped_true_in_prod(merged_values: dict[str, Any]) -> None:
    env = merged_values["services"]["api-gateway"]["env"]
    assert env.get("API_GATEWAY_AUTH_REQUIRED") == "true"


def test_rate_limit_enabled_flipped_true_in_prod(merged_values: dict[str, Any]) -> None:
    """RATE_LIMIT_ENABLED is set on the api-gateway (the ingress front door)."""
    env = merged_values["services"]["api-gateway"]["env"]
    assert env.get("RATE_LIMIT_ENABLED") == "true"


def test_ingress_enabled_in_prod(merged_values: dict[str, Any]) -> None:
    assert merged_values["ingress"]["enabled"] is True


def test_ingress_has_tls_in_prod(merged_values: dict[str, Any]) -> None:
    """Production ingress must terminate TLS."""
    tls = merged_values["ingress"].get("tls")
    assert tls, "ingress.tls must be configured in prod"
    # Either a list of TLS entries or a dict with enabled/hostName.
    if isinstance(tls, list):
        assert tls, "ingress.tls list is empty"
    else:
        assert tls.get("enabled") is not None, "ingress.tls.enabled must be set"


def test_jwt_auth_strict_true_in_prod(merged_values: dict[str, Any]) -> None:
    """global.jwtAuthStrict flips to true in prod (drops legacy internal-token)."""
    assert merged_values["global"]["jwtAuthStrict"] is True


def test_oidc_placeholders_set_in_prod(merged_values: dict[str, Any]) -> None:
    """OIDC fields are present in the prod overlay as placeholders to wire
    an IdP (Keycloak/Auth0/Okta)."""
    secrets = merged_values["global"]["secrets"]
    for field in ("oidcIssuer", "oidcClientId", "oidcAudience", "oidcJwksUrl"):
        assert field in secrets, f"missing {field} in prod secrets"
        assert secrets[field] is not None, f"{field} must be present (non-null) in prod"


def test_storage_class_name_set_in_prod(merged_values: dict[str, Any]) -> None:
    """A storageClassName placeholder is set so PVCs bind to a real class."""
    assert merged_values["global"].get("storageClassName"), (
        "global.storageClassName must be set in prod"
    )


# --------------------------------------------------------------------------- #
# Code default stays false (overlay must not touch code)
# --------------------------------------------------------------------------- #


def test_dev_values_keep_auth_required_false(dev_values: dict[str, Any]) -> None:
    """The dev overlay must keep AUTH open so dev/test traffic flows."""
    env = dev_values["services"]["api-gateway"]["env"]
    assert env.get("API_GATEWAY_AUTH_REQUIRED", "false") == "false"


# --------------------------------------------------------------------------- #
# Cycle 2: README documents the overlay
# --------------------------------------------------------------------------- #


def test_readme_mentions_values_prod() -> None:
    text = README_PATH.read_text()
    assert "values-prod.yaml" in text, "README must reference values-prod.yaml"


def test_readme_mentions_auth_required() -> None:
    text = README_PATH.read_text()
    assert "API_GATEWAY_AUTH_REQUIRED" in text, "README must mention API_GATEWAY_AUTH_REQUIRED flip"
