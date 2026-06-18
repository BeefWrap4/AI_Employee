"""Neo4j topology graph tests (spec P2/P3 §4 Neo4j).

The :class:`Neo4jTopologyClient` runs Cypher queries against a Neo4j
database to resolve base-station / cell / transport-link / upstream-
device dependencies.  Tests inject a :class:`FakeNeo4jDriver` so no
live database is required; the production path wires the official
``neo4j`` Python driver.
"""
from __future__ import annotations

import pytest
from ai_employee.rca_agent.topology import (
    SEED_CYPHER,
    Dependency,
    FakeNeo4jDriver,
    Neo4jTopologyClient,
    TopologyResult,
    build_topology_client,
)

# --------------------------------------------------------------------------- #
# Dependency / TopologyResult
# --------------------------------------------------------------------------- #


def test_dependency_to_dict() -> None:
    dep = Dependency(
        node_id="gNB-01", node_type="base_station", name="BJ-001",
        relationship="UPSTREAM", hops=1,
    )
    d = dep.to_dict()
    assert d["node_id"] == "gNB-01"
    assert d["relationship"] == "UPSTREAM"


def test_topology_result_empty() -> None:
    result = TopologyResult(site_id="BJ-001", dependencies=[])
    assert result.site_id == "BJ-001"
    assert result.dependency_count == 0
    assert result.to_dict()["dependencies"] == []


def test_topology_result_to_dict() -> None:
    result = TopologyResult(
        site_id="BJ-001",
        dependencies=[
            Dependency(node_id="SW-01", node_type="switch", name="Core-SW",
                       relationship="UPSTREAM", hops=1),
            Dependency(node_id="CELL-02", node_type="cell", name="BJ-001-C2",
                       relationship="NEIGHBOR", hops=1),
        ],
    )
    d = result.to_dict()
    assert d["dependency_count"] == 2
    assert d["dependencies"][0]["node_id"] == "SW-01"


# --------------------------------------------------------------------------- #
# FakeNeo4jDriver
# --------------------------------------------------------------------------- #


def test_fake_driver_seed_adds_nodes() -> None:
    driver = FakeNeo4jDriver()
    driver.seed([
        {"node_id": "gNB-01", "node_type": "base_station", "name": "BJ-001"},
    ])
    with driver.session() as s:
        rows = s.run("MATCH (n) RETURN n")
    assert len(rows) == 1


def test_fake_driver_run_returns_seeded_rows() -> None:
    driver = FakeNeo4jDriver()
    driver.seed([
        {"node_id": "gNB-01", "node_type": "base_station", "name": "BJ-001",
         "relationship": "UPSTREAM", "hops": 1},
    ])
    with driver.session() as s:
        rows = s.run("MATCH (n {site_id: 'BJ-001'}) RETURN n")
    assert rows[0]["node_id"] == "gNB-01"


# --------------------------------------------------------------------------- #
# Neo4jTopologyClient — query_upstream_dependencies
# --------------------------------------------------------------------------- #


def test_query_upstream_returns_dependencies() -> None:
    driver = FakeNeo4jDriver()
    driver.seed([
        {"node_id": "SW-01", "node_type": "switch", "name": "Core-SW",
         "relationship": "UPSTREAM", "hops": 1},
        {"node_id": "R-01", "node_type": "router", "name": "Core-RTR",
         "relationship": "UPSTREAM", "hops": 2},
    ])
    client = Neo4jTopologyClient(driver=driver)  # type: ignore[arg-type]
    result = client.query_upstream_dependencies(site_id="BJ-001")
    assert isinstance(result, TopologyResult)
    assert result.dependency_count == 2
    assert result.dependencies[0].node_type == "switch"


def test_query_upstream_empty_when_no_deps() -> None:
    driver = FakeNeo4jDriver()
    client = Neo4jTopologyClient(driver=driver)  # type: ignore[arg-type]
    result = client.query_upstream_dependencies(site_id="UNKNOWN")
    assert result.dependency_count == 0


def test_query_neighbors_returns_cells() -> None:
    driver = FakeNeo4jDriver()
    driver.seed([
        {"node_id": "CELL-02", "node_type": "cell", "name": "BJ-001-C2",
         "relationship": "NEIGHBOR", "hops": 1},
    ])
    client = Neo4jTopologyClient(driver=driver)  # type: ignore[arg-type]
    neighbors = client.query_neighbors(site_id="BJ-001")
    assert len(neighbors) == 1
    assert neighbors[0].node_type == "cell"


def test_client_to_evidence_payload() -> None:
    """The client can render its result as an RCA evidence payload."""
    driver = FakeNeo4jDriver()
    driver.seed([
        {"node_id": "SW-01", "node_type": "switch", "name": "Core-SW",
         "relationship": "UPSTREAM", "hops": 1},
    ])
    client = Neo4jTopologyClient(driver=driver)  # type: ignore[arg-type]
    result = client.query_upstream_dependencies(site_id="BJ-001")
    payload = client.to_evidence_payload(result)
    assert "BJ-001" in payload["content"]
    assert payload["source_type"] == "topology"
    assert payload["confidence"] > 0


# --------------------------------------------------------------------------- #
# SEED_CYPHER
# --------------------------------------------------------------------------- #


def test_seed_cypher_is_nonempty_string() -> None:
    assert isinstance(SEED_CYPHER, str)
    assert "CREATE" in SEED_CYPHER.upper() or "MERGE" in SEED_CYPHER.upper()


# --------------------------------------------------------------------------- #
# build_topology_client
# --------------------------------------------------------------------------- #


def test_build_client_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEO4J_URL", raising=False)
    assert build_topology_client() is None


def test_build_client_enabled_returns_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URL", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    import ai_employee.rca_agent.topology as topo

    monkeypatch.setattr(topo, "_connect_neo4j", lambda **kw: FakeNeo4jDriver())
    client = build_topology_client()
    assert client is not None
    assert isinstance(client, Neo4jTopologyClient)


def test_build_client_unreachable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URL", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    monkeypatch.setenv("NEO4J_TIMEOUT_S", "0.2")
    assert build_topology_client() is None
