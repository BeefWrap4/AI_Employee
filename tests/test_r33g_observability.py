"""R33-G1: Grafana dashboard + datasource provisioning + prometheus.yml.

Three TDD cycles:
  g1a — prometheus scrape config for all 9 services + self.
  g1b — Grafana datasource provisioning pointing at prometheus:9090.
  g1c — Grafana dashboard JSON with one panel per headline indicator
        (7 panels) + dashboard provider config + compose volume mount.

The seven headline indicators live in
``packages/common-schemas/src/ai_employee/common_schemas/metrics_bridge.py``
and are rendered by ``to_prometheus_text`` with a ``platform_`` prefix,
e.g. ``platform_agent_run_success_rate``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required for provisioning validation")

REPO_ROOT = Path(__file__).resolve().parents[1]
OBS_DIR = REPO_ROOT / "infra" / "observability"
PROM_YML = OBS_DIR / "prometheus.yml"
GRAFANA_DIR = OBS_DIR / "grafana"
DATASOURCE_YML = GRAFANA_DIR / "provisioning" / "datasources" / "prometheus.yml"
DASHBOARDS_YML = GRAFANA_DIR / "provisioning" / "dashboards" / "dashboards.yml"
DASHBOARD_JSON = GRAFANA_DIR / "provisioning" / "dashboards" / "agent-platform.json"
COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose" / "compose.yml"

# Canonical service -> scrape port mapping (CLAUDE.md service list).
SERVICE_PORTS: dict[str, str] = {
    "knowledge-api": "8010",
    "ingestion-worker": "8011",
    "rca-agent": "8020",
    "agent-platform-api": "8030",
    "tool-registry": "8040",
    "approval-service": "8040",
    "mcp-gateway": "8050",
    "event-gateway": "8060",
    "api-gateway": "8070",
}

# The seven headline indicators, exactly as rendered by
# metrics_bridge.to_prometheus_text (platform_<key> gauge).
INDICATOR_METRICS: list[str] = [
    "platform_agent_run_success_rate",
    "platform_approval_wait_time_p95_s",
    "platform_report_acceptance_rate",
    "platform_model_latency_p95_ms",
    "platform_tool_latency_p95_ms",
    "platform_fallback_rate",
    "platform_tool_call_success_rate",
]


# --------------------------------------------------------------------------- #
# Cycle g1a — prometheus.yml scrape config
# --------------------------------------------------------------------------- #


@pytest.fixture
def prom_yml() -> dict:
    assert PROM_YML.exists(), f"missing {PROM_YML}"
    with PROM_YML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_prom_yml_has_scrape_configs(prom_yml: dict) -> None:
    assert "scrape_configs" in prom_yml
    assert isinstance(prom_yml["scrape_configs"], list)
    assert len(prom_yml["scrape_configs"]) >= 1


def test_prom_yml_references_each_service_port(prom_yml: dict) -> None:
    """Every canonical service port must appear in at least one target."""
    blob = yaml.safe_dump(prom_yml)
    expected_ports = sorted(set(SERVICE_PORTS.values()))
    for port in expected_ports:
        assert port in blob, f"prometheus.yml missing scrape target port {port}"


def test_prom_yml_has_self_scrape_job(prom_yml: dict) -> None:
    """Prometheus must scrape itself (job_name == 'prometheus')."""
    job_names = {job.get("job_name") for job in prom_yml["scrape_configs"]}
    assert "prometheus" in job_names


def test_prom_yml_scrape_configs_use_static_configs(prom_yml: dict) -> None:
    for job in prom_yml["scrape_configs"]:
        assert "static_configs" in job, f"job {job.get('job_name')} missing static_configs"


# --------------------------------------------------------------------------- #
# Cycle g1b — Grafana datasource provisioning
# --------------------------------------------------------------------------- #


@pytest.fixture
def datasource_yml() -> dict:
    assert DATASOURCE_YML.exists(), f"missing {DATASOURCE_YML}"
    with DATASOURCE_YML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_datasource_yml_api_version(datasource_yml: dict) -> None:
    assert datasource_yml.get("apiVersion") == 1


def test_datasource_yml_has_prometheus_datasource(datasource_yml: dict) -> None:
    datasources = datasource_yml.get("datasources")
    assert isinstance(datasources, list)
    assert len(datasources) >= 1
    prom = next(
        (d for d in datasources if d.get("type") == "prometheus"),
        None,
    )
    assert prom is not None, "no prometheus datasource entry"
    assert prom.get("name") == "Prometheus"
    assert prom.get("access") == "proxy"
    assert prom.get("isDefault") is True


def test_datasource_yml_points_at_prometheus_9090(datasource_yml: dict) -> None:
    prom = next(d for d in datasource_yml["datasources"] if d.get("type") == "prometheus")
    assert prom.get("url") == "http://prometheus:9090"


# --------------------------------------------------------------------------- #
# Cycle g1c — Grafana dashboard JSON + provider + compose wiring
# --------------------------------------------------------------------------- #


@pytest.fixture
def dashboard_json() -> dict:
    assert DASHBOARD_JSON.exists(), f"missing {DASHBOARD_JSON}"
    with DASHBOARD_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def dashboards_yml() -> dict:
    assert DASHBOARDS_YML.exists(), f"missing {DASHBOARDS_YML}"
    with DASHBOARDS_YML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def compose_yml() -> dict:
    assert COMPOSE_PATH.exists(), f"missing {COMPOSE_PATH}"
    with COMPOSE_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_dashboard_json_parses(dashboard_json: dict) -> None:
    assert "title" in dashboard_json
    assert "panels" in dashboard_json
    assert isinstance(dashboard_json["panels"], list)


def test_dashboard_json_has_at_least_seven_panels(dashboard_json: dict) -> None:
    assert len(dashboard_json["panels"]) >= 7


def test_dashboard_json_each_panel_references_indicator(dashboard_json: dict) -> None:
    """Every panel must query one of the seven indicator metric names."""
    blob = json.dumps(dashboard_json)
    for metric in INDICATOR_METRICS:
        assert metric in blob, f"dashboard missing metric {metric}"
    # Each panel's expr should reference an indicator metric.
    for panel in dashboard_json["panels"]:
        targets = panel.get("targets", [])
        assert targets, f"panel {panel.get('title')} has no targets"
        joined = json.dumps(targets)
        assert any(m in joined for m in INDICATOR_METRICS), (
            f"panel {panel.get('title')} does not reference an indicator metric"
        )


def test_dashboard_provider_yml_parses(dashboards_yml: dict) -> None:
    assert dashboards_yml.get("apiVersion") == 1
    providers = dashboards_yml.get("providers")
    assert isinstance(providers, list)
    assert len(providers) >= 1
    prov = providers[0]
    assert prov.get("folder") != ""
    assert prov.get("options", {}).get("path") != ""


def test_compose_grafana_mounts_provisioning(compose_yml: dict) -> None:
    """The grafana service must bind-mount the provisioning dir."""
    grafana = compose_yml["services"].get("grafana")
    assert grafana is not None, "no grafana service in compose"
    volumes = grafana.get("volumes", [])
    assert any("provisioning" in v for v in volumes), (
        f"grafana service has no provisioning volume mount: {volumes}"
    )
