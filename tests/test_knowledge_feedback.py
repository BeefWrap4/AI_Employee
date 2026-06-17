from __future__ import annotations

from fastapi.testclient import TestClient

from ai_employee.rca_agent.app import create_app
from ai_employee.rca_agent.knowledge_feedback import generate_candidates_from_report
from ai_employee.rca_agent.schemas import (
    AlarmEvent,
    Evidence,
    Hypothesis,
    IncidentResponse,
    RcaReportResponse,
)


def _alarm(alarm_id: str, site_id: str, alarm_code: str = "LINK_DEGRADE") -> dict:
    return {
        "alarm_id": alarm_id,
        "alarm_code": alarm_code,
        "alarm_name": alarm_code.replace("_", " ").title(),
        "vendor": "huawei",
        "site_id": site_id,
        "cell_id": f"{site_id}-CELL",
        "ne_id": f"{site_id}-NE",
        "severity": "critical",
        "start_time": "2026-06-17T10:00:00+08:00",
        "raw_payload": {},
    }


def _create_run(client: TestClient, alarm_id: str, site_id: str) -> dict:
    response = client.post(
        "/api/v1/rca/runs",
        json={"alarms": [_alarm(alarm_id, site_id)], "require_human_review": True},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _accept_report(client: TestClient, report_id: str, root_cause: str = "传输链路劣化") -> dict:
    response = client.post(
        f"/api/v1/rca/reports/{report_id}/review",
        json={"decision": "accepted", "final_root_cause": root_cause},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Unit tests: generate_candidates_from_report split logic
# ---------------------------------------------------------------------------


def _build_report(hypotheses: list[Hypothesis], evidence: list[Evidence]) -> RcaReportResponse:
    return RcaReportResponse(
        report_id="rca_report_001",
        run_id="rca_run_001",
        incident_id="inc_001",
        report_markdown="md",
        hypotheses=hypotheses,
        evidence=evidence,
        review_status="accepted",
        final_root_cause="confirmed transmission link degradation",
    )


def _build_incident() -> IncidentResponse:
    return IncidentResponse(
        incident_id="inc_001",
        title="SITE-001 link degrade",
        status="analyzing",
        severity="critical",
        site_id="SITE-001",
        primary_alarm=AlarmEvent(
            alarm_id="a_001",
            alarm_code="LINK_DEGRADE",
            alarm_name="Transmission link degradation",
            vendor="huawei",
            site_id="SITE-001",
            cell_id="CELL-001",
            ne_id="NE-001",
            severity="critical",
            start_time="2026-06-17T10:00:00+08:00",
            alarm_event_id="alarm_evt_001",
            fingerprint="huawei:SITE-001:NE-001:LINK_DEGRADE",
        ),
        related_alarm_count=0,
        alarm_events=[],
    )


def test_generate_candidates_only_when_accepted_with_final_root_cause() -> None:
    hypotheses = [
        Hypothesis(
            hypothesis_id="h_001",
            root_cause_type="transmission_link_degradation",
            description="Transmission link degradation is the most likely root cause.",
            supporting_evidence_ids=["e_001", "e_003"],
            confidence=0.78,
            next_check=["Check port error counters.", "Check fiber maintenance records."],
        ),
        Hypothesis(
            hypothesis_id="h_002",
            root_cause_type="recent_parameter_change",
            description="Recent configuration changes could contribute.",
            supporting_evidence_ids=["e_002"],
            confidence=0.46,
            next_check=["Compare parameter changes."],
        ),
    ]
    evidence = [
        Evidence(
            evidence_id="e_001",
            source_type="metric",
            source_ref="kpi:SITE-001",
            content="RRC setup failure rate increased.",
            confidence=0.82,
        ),
        Evidence(
            evidence_id="e_002",
            source_type="log",
            source_ref="log:NE-001",
            content="NE logs include LINK_DEGRADE.",
            confidence=0.76,
        ),
        Evidence(
            evidence_id="e_003",
            source_type="topology",
            source_ref="topology:SITE-001",
            content="Affected cell depends on the same upstream path.",
            confidence=0.78,
        ),
    ]
    report = _build_report(hypotheses, evidence)
    incident = _build_incident()

    candidates = generate_candidates_from_report(report, incident, evidence)

    assert len(candidates) == 2
    cand_a = candidates[0]
    assert cand_a.source_report_id == "rca_report_001"
    assert cand_a.source_incident_id == "inc_001"
    assert cand_a.hypothesis_id == "h_001"
    assert cand_a.root_cause_type == "transmission_link_degradation"
    assert cand_a.title == "Transmission link degradation is the most likely root cause."
    assert cand_a.review_status == "pending"
    # content carries hypothesis description, final root cause, and next checks
    assert "Transmission link degradation" in cand_a.content
    assert "confirmed transmission link degradation" in cand_a.content
    assert "Check port error counters." in cand_a.content
    # evidence_summary joins supporting evidence source_type + content
    assert "metric" in cand_a.evidence_summary
    assert "RRC setup failure rate increased." in cand_a.evidence_summary
    assert "topology" in cand_a.evidence_summary
    assert "Affected cell depends on the same upstream path." in cand_a.evidence_summary
    # e_002 is not supporting h_001 -> should not appear
    assert "NE logs include LINK_DEGRADE." not in cand_a.evidence_summary

    cand_b = candidates[1]
    assert cand_b.hypothesis_id == "h_002"
    assert cand_b.root_cause_type == "recent_parameter_change"
    assert "log" in cand_b.evidence_summary
    assert "NE logs include LINK_DEGRADE." in cand_b.evidence_summary


def test_generate_candidates_title_truncated_to_80_chars() -> None:
    long_desc = "A" * 120
    hypotheses = [
        Hypothesis(
            hypothesis_id="h_001",
            root_cause_type="transmission_link_degradation",
            description=long_desc,
            supporting_evidence_ids=[],
            confidence=0.7,
            next_check=[],
        ),
    ]
    report = _build_report(hypotheses, [])
    candidates = generate_candidates_from_report(report, _build_incident(), [])
    assert len(candidates) == 1
    assert len(candidates[0].title) == 80
    assert candidates[0].title == "A" * 80


def test_generate_candidates_returns_empty_when_not_accepted() -> None:
    report = _build_report([], [])
    report = report.model_copy(update={"review_status": "rejected"})
    candidates = generate_candidates_from_report(report, _build_incident(), [])
    assert candidates == []


def test_generate_candidates_returns_empty_when_final_root_cause_missing() -> None:
    report = _build_report(
        [
            Hypothesis(
                hypothesis_id="h_001",
                root_cause_type="t",
                description="d",
                supporting_evidence_ids=[],
                confidence=0.5,
                next_check=[],
            )
        ],
        [],
    )
    report = report.model_copy(update={"final_root_cause": None})
    candidates = generate_candidates_from_report(report, _build_incident(), [])
    assert candidates == []


# ---------------------------------------------------------------------------
# Integration tests via API: auto-trigger + list/filter/pagination + review
# ---------------------------------------------------------------------------


def test_accepting_report_auto_generates_candidates() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_001", "SITE-001")
    report = client.get(f"/api/v1/rca/reports/{created['report_id']}").json()
    hypothesis_count = len(report["hypotheses"])

    _accept_report(client, created["report_id"], root_cause="confirmed link degradation")

    listed = client.get("/api/v1/candidate-knowledge")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == hypothesis_count
    for item in body["items"]:
        assert item["review_status"] == "pending"
        assert item["source_report_id"] == created["report_id"]
        assert item["source_incident_id"] == created["incident_id"]
        assert item["imported_doc_id"] is None


def test_rejected_report_generates_no_candidates() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_002", "SITE-002")
    client.post(
        f"/api/v1/rca/reports/{created['report_id']}/review",
        json={"decision": "rejected", "comment": "no"},
    )
    listed = client.get("/api/v1/candidate-knowledge")
    assert listed.json()["total"] == 0


def test_accepted_without_final_root_cause_generates_no_candidates() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_003", "SITE-003")
    client.post(
        f"/api/v1/rca/reports/{created['report_id']}/review",
        json={"decision": "accepted"},
    )
    listed = client.get("/api/v1/candidate-knowledge")
    assert listed.json()["total"] == 0


def test_list_candidates_filter_by_review_status_and_incident_id() -> None:
    client = TestClient(create_app())
    one = _create_run(client, "a_010", "SITE-010")
    two = _create_run(client, "a_011", "SITE-011")
    _accept_report(client, one["report_id"], root_cause="root one")
    _accept_report(client, two["report_id"], root_cause="root two")

    by_incident = client.get(f"/api/v1/candidate-knowledge?incident_id={one['incident_id']}")
    assert by_incident.status_code == 200
    assert by_incident.json()["total"] == len(
        client.get(f"/api/v1/rca/reports/{one['report_id']}").json()["hypotheses"]
    )
    assert all(
        item["source_incident_id"] == one["incident_id"]
        for item in by_incident.json()["items"]
    )

    by_status = client.get("/api/v1/candidate-knowledge?review_status=pending")
    assert by_status.json()["total"] >= 2


def test_list_candidates_pagination() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_020", "SITE-020")
    _accept_report(client, created["report_id"], root_cause="root")
    total = client.get("/api/v1/candidate-knowledge").json()["total"]
    assert total >= 2

    page = client.get("/api/v1/candidate-knowledge?page=1&page_size=1")
    assert page.json()["total"] == total
    assert len(page.json()["items"]) == 1
    assert page.json()["page"] == 1
    assert page.json()["page_size"] == 1

    page2 = client.get("/api/v1/candidate-knowledge?page=2&page_size=1")
    assert len(page2.json()["items"]) == 1


def test_get_candidate_detail_and_404() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_030", "SITE-030")
    _accept_report(client, created["report_id"], root_cause="root")
    candidate_id = client.get("/api/v1/candidate-knowledge").json()["items"][0]["candidate_id"]

    detail = client.get(f"/api/v1/candidate-knowledge/{candidate_id}")
    assert detail.status_code == 200
    assert detail.json()["candidate_id"] == candidate_id

    missing = client.get("/api/v1/candidate-knowledge/ck_unknown")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "candidate_not_found"


def test_review_candidate_approved_and_rejected_transitions() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_040", "SITE-040")
    _accept_report(client, created["report_id"], root_cause="root")
    items = client.get("/api/v1/candidate-knowledge").json()["items"]
    approved_id = items[0]["candidate_id"]
    rejected_id = items[1]["candidate_id"]

    a = client.post(
        f"/api/v1/candidate-knowledge/{approved_id}/review",
        json={"decision": "approved", "reviewer": "expert_01", "comment": "correct"},
    )
    assert a.status_code == 200, a.text
    assert a.json()["review_status"] == "approved"
    assert a.json()["reviewer"] == "expert_01"
    assert a.json()["review_comment"] == "correct"
    assert a.json()["reviewed_at"] is not None

    r = client.post(
        f"/api/v1/candidate-knowledge/{rejected_id}/review",
        json={"decision": "rejected", "reviewer": "expert_02", "comment": "wrong"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "rejected"

    detail = client.get(f"/api/v1/candidate-knowledge/{approved_id}").json()
    assert detail["review_status"] == "approved"


def test_review_candidate_already_reviewed_returns_409() -> None:
    client = TestClient(create_app())
    created = _create_run(client, "a_050", "SITE-050")
    _accept_report(client, created["report_id"], root_cause="root")
    candidate_id = client.get("/api/v1/candidate-knowledge").json()["items"][0]["candidate_id"]

    first = client.post(
        f"/api/v1/candidate-knowledge/{candidate_id}/review",
        json={"decision": "approved", "reviewer": "expert_01"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/candidate-knowledge/{candidate_id}/review",
        json={"decision": "rejected", "reviewer": "expert_02"},
    )
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "already_reviewed"
