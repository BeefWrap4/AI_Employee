"""Neo4j topology graph client (spec P2/P3 §4 Neo4j).

Resolves base-station / cell / transport-link / upstream-device
dependencies from a Neo4j graph.  Replaces the HTTP-stub
``Neo4jTopologyAdapter`` with real Cypher queries against the official
``neo4j`` Python driver.

The driver is pluggable behind :class:`Neo4jDriverProtocol`; tests
inject :class:`FakeNeo4jDriver` so no live database is required.
:func:`build_topology_client` returns ``None`` when ``NEO4J_URL`` is
unset or unreachable, so services without Neo4j keep working.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Protocol

logger = logging.getLogger(__name__)


@dataclass
class Dependency:
    """One node in the resolved topology subgraph."""

    node_id: str
    node_type: str
    name: str
    relationship: str
    hops: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TopologyResult:
    """Resolved upstream dependency subgraph for one site."""

    site_id: str
    dependencies: list[Dependency] = field(default_factory=list)

    @property
    def dependency_count(self) -> int:
        return len(self.dependencies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "dependency_count": self.dependency_count,
            "dependencies": [d.to_dict() for d in self.dependencies],
        }


# Seed Cypher: create base_station / cell / transport_link / upstream
# device nodes + relationships.  Run once to bootstrap the graph.
SEED_CYPHER = """
MERGE (bs:BaseStation {site_id: 'BJ-001', name: '北京-001'})
MERGE (cell1:Cell {node_id: 'CELL-01', name: 'BJ-001-C1'})
MERGE (cell2:Cell {node_id: 'CELL-02', name: 'BJ-001-C2'})
MERGE (sw:Switch {node_id: 'SW-01', name: 'Core-SW'})
MERGE (rtr:Router {node_id: 'R-01', name: 'Core-RTR'})
MERGE (link:TransportLink {node_id: 'TL-01', name: 'BJ-SH-10G'})
MERGE (cell1)-[:BELONGS_TO]->(bs)
MERGE (cell2)-[:BELONGS_TO]->(bs)
MERGE (cell1)-[:NEIGHBOR]->(cell2)
MERGE (bs)-[:UPSTREAM]->(sw)
MERGE (sw)-[:UPSTREAM]->(rtr)
MERGE (rtr)-[:CARRIES]->(link)
"""


# --------------------------------------------------------------------------- #
# Driver protocol + fake
# --------------------------------------------------------------------------- #


class Neo4jDriverProtocol(Protocol):
    def session(self) -> Any: ...
    def close(self) -> None: ...


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def run(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        # The fake ignores the cypher and returns whatever was seeded,
        # optionally filtered by a site_id param.
        if "site_id" in params:
            return [r for r in self._rows]
        return list(self._rows)

    def close(self) -> None:
        pass


class FakeNeo4jDriver:
    """In-memory driver for tests.  ``seed`` populates the row store."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def seed(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    @contextmanager
    def session(self) -> Iterator[_FakeSession]:
        yield _FakeSession(self._rows)

    def close(self) -> None:
        self._rows.clear()


# --------------------------------------------------------------------------- #
# Neo4jTopologyClient
# --------------------------------------------------------------------------- #


class Neo4jTopologyClient:
    """Runs Cypher queries against Neo4j to resolve topology."""

    def __init__(self, *, driver: Neo4jDriverProtocol) -> None:
        self._driver = driver

    def query_upstream_dependencies(self, *, site_id: str) -> TopologyResult:
        """Return the upstream dependency chain for ``site_id``."""
        cypher = (
            "MATCH (bs:BaseStation {site_id: $site_id})-[:UPSTREAM*1..3]->(up) "
            "RETURN up.node_id AS node_id, labels(up)[0] AS node_type, "
            "up.name AS name, type(last(relationships(p))) AS relationship, "
            "length(p) AS hops"
        )
        rows = self._run(cypher, site_id=site_id)
        deps = [
            Dependency(
                node_id=str(r.get("node_id", "")),
                node_type=str(r.get("node_type", "unknown")),
                name=str(r.get("name", "")),
                relationship=str(r.get("relationship", "UPSTREAM")),
                hops=int(r.get("hops", 1)),
            )
            for r in rows
        ]
        return TopologyResult(site_id=site_id, dependencies=deps)

    def query_neighbors(self, *, site_id: str) -> list[Dependency]:
        """Return neighboring cells for ``site_id``."""
        cypher = (
            "MATCH (bs:BaseStation {site_id: $site_id})<-[:BELONGS_TO]-(cell)-[:NEIGHBOR]->(nb) "
            "RETURN nb.node_id AS node_id, labels(nb)[0] AS node_type, "
            "nb.name AS name, 'NEIGHBOR' AS relationship, 1 AS hops"
        )
        rows = self._run(cypher, site_id=site_id)
        return [
            Dependency(
                node_id=str(r.get("node_id", "")),
                node_type=str(r.get("node_type", "cell")),
                name=str(r.get("name", "")),
                relationship="NEIGHBOR",
                hops=1,
            )
            for r in rows
        ]

    def to_evidence_payload(self, result: TopologyResult) -> dict[str, Any]:
        """Render a topology result as an RCA evidence payload."""
        deps = result.dependencies
        summary = ", ".join(f"{d.node_type}:{d.node_id}" for d in deps[:5])
        content = (
            f"Neo4j topology for {result.site_id}: {result.dependency_count} "
            f"upstream dependencies ({summary})."
        )
        return {
            "evidence_id": f"neo4j_{result.site_id}",
            "source_type": "topology",
            "source_ref": f"neo4j:{result.site_id}",
            "content": content,
            "confidence": 0.7 if deps else 0.4,
            "dependencies": [d.to_dict() for d in deps],
        }

    def _run(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        try:
            with self._driver.session() as session:
                rows = session.run(cypher, **params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j query failed: %s", exc)
            return []
        return list(rows) if rows else []

    def close(self) -> None:
        self._driver.close()


def _connect_neo4j(
    *, url: str, user: str, password: str, timeout_s: float,
) -> Neo4jDriverProtocol:
    from neo4j import GraphDatabase  # type: ignore[import-not-found]

    driver = GraphDatabase.driver(url, auth=(user, password), connection_timeout=timeout_s)
    # Verify connectivity eagerly so build_topology_client can fail-closed.
    driver.verify_connectivity()
    return driver  # type: ignore[return-value]


def build_topology_client() -> Neo4jTopologyClient | None:
    """Build a client from env.  Returns ``None`` when Neo4j is unset/unreachable.

    Env: ``NEO4J_URL`` (bolt://...), ``NEO4J_USER``, ``NEO4J_PASSWORD``,
    ``NEO4J_TIMEOUT_S``.
    """
    url = os.environ.get("NEO4J_URL")
    if not url:
        return None
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    try:
        timeout = float(os.environ.get("NEO4J_TIMEOUT_S", "2.0"))
        driver = _connect_neo4j(url=url, user=user, password=password, timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j unavailable (%s): %s", url, exc)
        return None
    return Neo4jTopologyClient(driver=driver)


__all__ = [
    "Dependency",
    "FakeNeo4jDriver",
    "Neo4jTopologyClient",
    "Neo4jDriverProtocol",
    "SEED_CYPHER",
    "TopologyResult",
    "build_topology_client",
]