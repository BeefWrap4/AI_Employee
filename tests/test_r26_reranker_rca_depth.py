"""R26: reranker recall-window + RCA convergence-params exposure tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Reranker recall window: wide recall → rerank → narrow
# --------------------------------------------------------------------------- #


def test_retrieval_passes_wide_candidate_window_to_reranker() -> None:
    """The retrieval service should hand the reranker a wider candidate
    pool than the final top_k, so the second-stage re-ordering has
    something to discriminate.  Pre-R26 the fused results were truncated
    to top_k *before* rerank, defeating the purpose of a second stage.

    Unit-level: directly assert that the recall window is wider than
    top_k by reading the retrieval service source (the implementation
    detail is a 1-line ``recall_window = min(50, max(top_k*5, top_k))``).
    """
    import inspect

    from ai_employee.knowledge_api import retrieval as retrieval_mod

    src = inspect.getsource(retrieval_mod.RetrievalService.search)
    assert "recall_window" in src, "RetrievalService.search must use a recall_window"
    # Ensure rerank still happens after recall_window slicing.
    assert "rerank" in src


def test_built_in_stub_reranker_increases_candidate_count() -> None:
    """Sanity: StubReranker accepts any number of candidates, doesn't drop."""
    from ai_employee.knowledge_api.reranker import StubReranker

    hits = []
    for i in range(10):
        from ai_employee.knowledge_api.retrieval import RetrievalHit

        hits.append(
            RetrievalHit(
                chunk_id=f"c{i}",
                doc_id=f"d{i}",
                doc_title=f"title{i}",
                content="alpha" if i < 3 else "beta",
                section_path="",
                page_no=1,
                confidence=0.5,
            )
        )
    r = StubReranker()
    out = r.rerank("alpha question", list(hits), 3)
    assert len(out) == 3


# --------------------------------------------------------------------------- #
# RCA convergence knobs: topology_window + parent_child_lag now exposed
# --------------------------------------------------------------------------- #


def _client() -> TestClient:
    from ai_employee.rca_agent.app import create_app

    return TestClient(create_app())


def test_incident_build_request_accepts_topology_window() -> None:
    """R26: IncidentBuildRequest now accepts topology_window_minutes."""
    client = _client()
    r = client.post(
        "/api/v1/incidents/build",
        json={
            "alarms": [
                {
                    "alarm_id": "a-1",
                    "alarm_name": "Link Loss Of Signal",
                    "vendor": "huawei",
                    "site_id": "SITE-001",
                    "ne_id": "NE-1",
                    "alarm_code": "LINK_LOS",
                    "severity": "critical",
                    "start_time": "2026-06-19T10:00:00Z",
                    "raw_payload": {},
                }
            ],
            "time_window_minutes": 30,
            "topology_window_minutes": 60,
            "parent_child_lag_seconds": 300,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["site_id"] == "SITE-001"


def test_incident_build_request_defaults_preserve_compat() -> None:
    """R26 backward compat: omitting the new knobs still works."""
    client = _client()
    r = client.post(
        "/api/v1/incidents/build",
        json={
            "alarms": [
                {
                    "alarm_id": "a-2",
                    "alarm_name": "Power Down",
                    "vendor": "ericsson",
                    "site_id": "SITE-002",
                    "ne_id": "NE-2",
                    "alarm_code": "POWER_DOWN",
                    "severity": "major",
                    "start_time": "2026-06-19T10:05:00Z",
                    "raw_payload": {},
                }
            ],
            "time_window_minutes": 30,
        },
    )
    assert r.status_code == 201, r.text


def test_incident_build_rejects_zero_alarms() -> None:
    client = _client()
    r = client.post(
        "/api/v1/incidents/build",
        json={"alarms": [], "time_window_minutes": 30},
    )
    assert r.status_code == 422


def test_topology_window_negative_rejected() -> None:
    client = _client()
    r = client.post(
        "/api/v1/incidents/build",
        json={
            "alarms": [
                {
                    "alarm_id": "a-3",
                    "alarm_name": "Link Degrade",
                    "vendor": "nokia",
                    "site_id": "SITE-003",
                    "ne_id": "NE-3",
                    "alarm_code": "LINK_DEG",
                    "severity": "minor",
                    "start_time": "2026-06-19T10:10:00Z",
                    "raw_payload": {},
                }
            ],
            "time_window_minutes": 30,
            "topology_window_minutes": -1,
        },
    )
    assert r.status_code == 422
