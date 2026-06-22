"""Prometheus rules + Alertmanager config validation."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required for rule validation")


REPO_ROOT = Path(__file__).resolve().parents[1]
PROM_RULES_PATH = REPO_ROOT / "infra" / "observability" / "prometheus_rules.yaml"
ALERTMANAGER_PATH = REPO_ROOT / "infra" / "observability" / "alertmanager.yml"


# --------------------------------------------------------------------------- #
# prometheus_rules.yaml
# --------------------------------------------------------------------------- #


@pytest.fixture
def prom_rules() -> dict:
    assert PROM_RULES_PATH.exists(), f"missing {PROM_RULES_PATH}"
    with PROM_RULES_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_prom_rules_has_groups(prom_rules: dict) -> None:
    assert "groups" in prom_rules
    assert isinstance(prom_rules["groups"], list)
    assert len(prom_rules["groups"]) >= 1


def test_prom_rules_groups_have_alerts(prom_rules: dict) -> None:
    for group in prom_rules["groups"]:
        assert "name" in group
        assert "rules" in group
        for rule in group["rules"]:
            assert "alert" in rule
            assert "expr" in rule
            assert "for" in rule or "annotations" in rule


def test_prom_rules_include_core_alerts(prom_rules: dict) -> None:
    """The headline alerts must be present (spec §6.4)."""
    alert_names = {
        rule["alert"]
        for group in prom_rules["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }
    expected = {
        "HighModelLatency",
        "HighErrorRate",
        "LowToolSuccessRate",
        "ApprovalBacklog",
        "DependencyDown",
    }
    missing = expected - alert_names
    assert not missing, f"missing alert rules: {sorted(missing)}"


def test_prom_rules_alerts_have_severity_label(prom_rules: dict) -> None:
    for group in prom_rules["groups"]:
        for rule in group["rules"]:
            if "alert" not in rule:
                continue
            labels = rule.get("labels", {})
            assert "severity" in labels, f"alert {rule['alert']} missing severity label"


def test_prom_rules_alerts_have_runbook(prom_rules: dict) -> None:
    for group in prom_rules["groups"]:
        for rule in group["rules"]:
            if "alert" not in rule:
                continue
            annotations = rule.get("annotations", {})
            assert "summary" in annotations
            assert "runbook_url" in annotations


# --------------------------------------------------------------------------- #
# alertmanager.yml
# --------------------------------------------------------------------------- #


@pytest.fixture
def alertmanager() -> dict:
    assert ALERTMANAGER_PATH.exists(), f"missing {ALERTMANAGER_PATH}"
    with ALERTMANAGER_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_alertmanager_has_route(alertmanager: dict) -> None:
    assert "route" in alertmanager
    assert "receiver" in alertmanager["route"]


def test_alertmanager_has_receivers(alertmanager: dict) -> None:
    assert "receivers" in alertmanager
    assert isinstance(alertmanager["receivers"], list)
    names = {r["name"] for r in alertmanager["receivers"]}
    assert "default" in names


def test_alertmanager_route_groups_by_severity(alertmanager: dict) -> None:
    """Top-level route should fan out by severity label."""
    route = alertmanager["route"]
    routes = route.get("routes", [])
    group_by = []
    for r in routes:
        if r.get("group_by"):
            group_by.extend(r["group_by"])
    assert "severity" in group_by
