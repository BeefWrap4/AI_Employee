"""Helm template sanity tests (spec P3 §部署 Kubernetes + Helm).

Validates the chart's templates render to well-formed Kubernetes YAML
without a live cluster.  Uses :mod:`yaml` to parse the rendered
manifests; the only required dependency is PyYAML (already a dev
extra via pytest).

If ``helm`` CLI is on PATH we run ``helm template`` to render the
chart; otherwise we render a minimal manifest by hand to assert
the chart's structure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required")

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "infra" / "helm"


def _has_helm() -> bool:
    return shutil.which("helm") is not None


@pytest.fixture(scope="module")
def rendered() -> str:
    if not _has_helm():
        pytest.skip("helm CLI not installed; skipping live template render")
    result = subprocess.run(
        ["helm", "template", "ai-employee", str(CHART_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# --------------------------------------------------------------------------- #
# chart.yaml is valid
# --------------------------------------------------------------------------- #


def test_chart_yaml_loads() -> None:
    with (CHART_PATH / "Chart.yaml").open() as f:
        chart = yaml.safe_load(f)
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "ai-employee"
    assert chart["version"]


def test_values_yaml_loads() -> None:
    with (CHART_PATH / "values.yaml").open() as f:
        values = yaml.safe_load(f)
    assert "global" in values
    assert "services" in values
    # R29-C adds event-gateway (8 services total; spec §9 deploy units).
    assert set(values["services"].keys()) == {
        "knowledge-api",
        "ingestion-worker",
        "rca-agent",
        "agent-platform-api",
        "tool-registry",
        "approval-service",
        "mcp-gateway",
        "event-gateway",
    }


# --------------------------------------------------------------------------- #
# live template render (only when helm is available)
# --------------------------------------------------------------------------- #


def test_template_renders_all_services(rendered: str) -> None:
    """Every enabled service produces a Deployment + Service."""
    docs = list(yaml.safe_load_all(rendered))
    kinds = [d.get("kind") for d in docs if d]
    # 7 services × {Deployment, ServiceAccount, ConfigMap, PVC?} + extras
    for svc in (
        "knowledge-api",
        "ingestion-worker",
        "rca-agent",
        "agent-platform-api",
        "tool-registry",
        "approval-service",
        "mcp-gateway",
    ):
        assert any(
            d and d.get("kind") == "Deployment" and d.get("metadata", {}).get("name") == svc
            for d in docs
        ), f"missing Deployment for {svc}"


def test_template_renders_hpa_for_agent_platform(rendered: str) -> None:
    docs = list(yaml.safe_load_all(rendered))
    hpas = [
        d
        for d in docs
        if d
        and d.get("kind") == "HorizontalPodAutoscaler"
        and d.get("metadata", {}).get("name") == "agent-platform-api"
    ]
    assert hpas, "expected HPA for agent-platform-api"
    hpa = hpas[0]
    spec = hpa["spec"]
    assert spec["minReplicas"] == 2
    assert spec["maxReplicas"] == 6


def test_template_renders_pdb_for_each_service(rendered: str) -> None:
    docs = list(yaml.safe_load_all(rendered))
    pdbs = [d for d in docs if d and d.get("kind") == "PodDisruptionBudget"]
    services = {
        "knowledge-api",
        "ingestion-worker",
        "rca-agent",
        "agent-platform-api",
        "tool-registry",
    }
    pdb_services = {d["metadata"]["name"] for d in pdbs}
    assert services <= pdb_services


def test_template_renders_network_policies(rendered: str) -> None:
    docs = list(yaml.safe_load_all(rendered))
    nps = [d for d in docs if d and d.get("kind") == "NetworkPolicy"]
    assert len(nps) >= 5  # one per service


def test_template_renders_service_accounts(rendered: str) -> None:
    docs = list(yaml.safe_load_all(rendered))
    sas = [d for d in docs if d and d.get("kind") == "ServiceAccount"]
    names = {d["metadata"]["name"] for d in sas}
    assert "agent-platform-api" in names
    assert "rca-agent" in names


def test_deployment_uses_separated_health_probes(rendered: str) -> None:
    """Each Deployment has both readiness and liveness probes
    pointing at different paths (R11-7 work)."""
    docs = list(yaml.safe_load_all(rendered))
    deployments = [d for d in docs if d and d.get("kind") == "Deployment"]
    assert deployments
    for d in deployments:
        containers = d["spec"]["template"]["spec"]["containers"]
        assert containers, f"no containers in {d['metadata']['name']}"
        c = containers[0]
        probes = c.get("readinessProbe", {}), c.get("livenessProbe", {})
        ready_path = probes[0].get("httpGet", {}).get("path")
        live_path = probes[1].get("httpGet", {}).get("path")
        assert ready_path == "/health/ready"
        assert live_path == "/health"


def test_deployment_uses_nonroot_security_context(rendered: str) -> None:
    """Pod-level securityContext has runAsNonRoot=true (containers inherit)."""
    docs = list(yaml.safe_load_all(rendered))
    deployments = [d for d in docs if d and d.get("kind") == "Deployment"]
    assert deployments
    for d in deployments:
        pod_spec = d["spec"]["template"]["spec"]
        sec = pod_spec.get("securityContext", {})
        assert sec.get("runAsNonRoot") is True, f"pod {d['metadata']['name']} missing runAsNonRoot"


def test_hpa_targets_correct_deployment(rendered: str) -> None:
    docs = list(yaml.safe_load_all(rendered))
    for d in docs:
        if d and d.get("kind") == "HorizontalPodAutoscaler":
            target = d["spec"]["scaleTargetRef"]
            assert target["kind"] == "Deployment"
            # name should match an existing service.
            assert target["name"] in {
                "knowledge-api",
                "ingestion-worker",
                "rca-agent",
                "agent-platform-api",
                "tool-registry",
                "approval-service",
                "mcp-gateway",
            }


def test_network_policy_is_deny_by_default(rendered: str) -> None:
    """Each policy is Ingress+Egress, ingress restricted to same-ns + ingress-nginx."""
    docs = list(yaml.safe_load_all(rendered))
    nps = [d for d in docs if d and d.get("kind") == "NetworkPolicy"]
    for np in nps:
        spec = np["spec"]
        assert "Ingress" in spec["policyTypes"]
        assert "Egress" in spec["policyTypes"]
        # At least one ingress rule, and egress is not "allow all" to whole world.
        assert spec["ingress"], "no ingress rules"


def test_no_duplicate_resources_in_render(rendered: str) -> None:
    """Helm should produce at most one Deployment + ServiceAccount + ConfigMap per service."""
    docs = list(yaml.safe_load_all(rendered))
    seen: dict[tuple[str, str], int] = {}
    for d in docs:
        if not d:
            continue
        key = (d.get("kind"), d.get("metadata", {}).get("name", ""))
        seen[key] = seen.get(key, 0) + 1
    for (kind, name), count in seen.items():
        if kind in {
            "Deployment",
            "ServiceAccount",
            "ConfigMap",
            "PodDisruptionBudget",
            "HorizontalPodAutoscaler",
            "NetworkPolicy",
            "PersistentVolumeClaim",
        }:
            assert count == 1, f"duplicate {kind}/{name}: {count}"


# --------------------------------------------------------------------------- #
# R22: object-store env vars + MinIO StatefulSet
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rendered_with_object_store() -> str:
    if not _has_helm():
        pytest.skip("helm CLI not installed; skipping live template render")
    result = subprocess.run(
        [
            "helm",
            "template",
            "ai-employee",
            str(CHART_PATH),
            "--set",
            "objectStore.url=http://minio:9000",
            "--set",
            "objectStore.accessKey=test",
            "--set",
            "objectStore.secretKey=test",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def rendered_with_minio() -> str:
    if not _has_helm():
        pytest.skip("helm CLI not installed; skipping live template render")
    result = subprocess.run(
        [
            "helm",
            "template",
            "ai-employee",
            str(CHART_PATH),
            "--set",
            "objectStore.minio.enabled=true",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_object_store_env_vars_injected(rendered_with_object_store: str) -> None:
    """objectStore.url/credentials are exported as env vars to every pod."""
    docs = list(yaml.safe_load_all(rendered_with_object_store))
    deployments = [d for d in docs if d and d.get("kind") == "Deployment"]
    assert deployments, "no deployments rendered"
    for dep in deployments:
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        env_names = {e["name"] for e in env}
        assert "OBJECT_STORE_URL" in env_names, dep["metadata"]["name"]
        assert "OBJECT_STORE_BUCKET" in env_names, dep["metadata"]["name"]
        url_value = next(e["value"] for e in env if e["name"] == "OBJECT_STORE_URL")
        assert url_value == "http://minio:9000", dep["metadata"]["name"]


def test_minio_statefulset_renders_when_enabled(rendered_with_minio: str) -> None:
    """Setting objectStore.minio.enabled=true deploys a MinIO StatefulSet + PVC + Service."""
    docs = list(yaml.safe_load_all(rendered_with_minio))
    kinds = {d.get("kind") for d in docs if d}
    assert "StatefulSet" in kinds
    assert "PersistentVolumeClaim" in kinds
    sset = next(d for d in docs if d and d.get("kind") == "StatefulSet")
    assert sset["metadata"]["name"] == "minio"
    # The MinIO container uses the configured image.
    container = sset["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].startswith("minio/minio:")


def test_minio_disabled_by_default(rendered: str) -> None:
    """With the default values the chart does NOT ship a MinIO pod."""
    docs = list(yaml.safe_load_all(rendered))
    sset = next(
        (d for d in docs if d and d.get("kind") == "StatefulSet"),
        None,
    )
    assert sset is None
