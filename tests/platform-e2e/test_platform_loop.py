"""Platform end-to-end loop tests.

Drive the full AI Employee loop across multiple services in a single
process using FastAPI TestClient. Each test wires up the services'
in-memory stores (with SQLite where persistence matters) and exercises
the cross-service contracts:

  knowledge ingest → publish → query
  RCA run → review (accept) → candidate knowledge → import
  agent run → approval → resume
  eval run → compare
  tool register → invoke
  inspection

These are contract tests: they verify the public HTTP surface and the
data that flows between services, not internal implementation details.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from ai_employee.agent_platform_api.app import create_app as create_platform_app
from ai_employee.agent_platform_api.eval_store import EvalStore
from ai_employee.agent_platform_api.run_store import AgentRunStore
from ai_employee.auth_policy import issue_token
from ai_employee.common_schemas.eval import UnifiedReport
from ai_employee.ingestion_worker.app import create_app as create_worker_app
from ai_employee.knowledge_api.app import create_app as create_knowledge_app
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult
from ai_employee.rca_agent.app import create_app as create_rca_app
from ai_employee.rca_agent.runtime import RcaStore
from ai_employee.tool_registry.app import create_app as create_tools_app
from ai_employee.tool_registry.store import ToolRegistryStore

SECRET = "e2e-test-secret-please-rotate-32bytes!!"


class _InProcessWorker(WorkerClient):
    """Drive the real ingestion-worker app via TestClient (no HTTP server)."""

    def __init__(self) -> None:
        self._client = TestClient(create_worker_app())

    def health(self) -> bool:
        return True

    def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
        resp = self._client.post(
            "/internal/parse",
            json={
                "doc_id": doc_id,
                "file_path": file_path,
                "mime_type": mime_type,
                "metadata": metadata,
            },
        )
        if resp.status_code == 200:
            from ai_employee.common_schemas.knowledge import ParseResponse

            return WorkerDispatchResult(
                dispatched=True,
                dispatch_status="accepted",
                response=ParseResponse(**resp.json()),
            )
        return WorkerDispatchResult(
            dispatched=False,
            dispatch_status="worker_error",
            error=f"worker returned {resp.status_code}: {resp.text}",
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("INTERNAL_TOKEN", "e2e-internal-token")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("LLM_GATEWAY_ENABLED", "false")
    monkeypatch.delenv("JWT_AUTH_STRICT", raising=False)
    # Knowledge data dir MUST match what the test store uses so the
    # worker's path guard accepts uploaded files written under raw/.
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(tmp_path / "kdata"))
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(tmp_path / "platform"))
    monkeypatch.setenv("RCA_DATA_DIR", str(tmp_path / "rca"))
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "inspections"))


def _admin_headers() -> dict[str, str]:
    token = issue_token(subject="e2e-admin", roles=["admin"], secret=SECRET)
    return {"Authorization": f"Bearer {token}"}


def _operator_headers() -> dict[str, str]:
    token = issue_token(
        subject="e2e-operator", roles=["operator"],
        scopes=["tool:invoke", "knowledge:read", "knowledge:write"],
        secret=SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Knowledge loop: ingest → publish → query
# --------------------------------------------------------------------------- #


def test_knowledge_ingest_publish_query_loop(tmp_path) -> None:
    data_dir = tmp_path / "kdata"
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(
        db_path=str(tmp_path / "k.sqlite3"), data_dir=str(data_dir),
    )
    store.init_schema()
    client = TestClient(create_knowledge_app(store=store, worker_client=_InProcessWorker()))

    # Upload a markdown SOP (worker parses synchronously → ready).
    upload = client.post(
        "/api/v1/documents",
        data={
            "title": "RRC 排障 SOP",
            "metadata_json": json.dumps({"network_type": "5g"}),
            "acl_tags_json": json.dumps(["wireless"]),
            "version": "v1",
            "mime_type": "text/markdown",
        },
        files={"file": (
            "sop.md",
            "# RRC\n\nRRC 建立失败先查告警 KPI。".encode("utf-8"),
            "text/markdown",
        )},
    )
    assert upload.status_code == 202, upload.text
    doc_id = upload.json()["doc_id"]
    assert upload.json()["parse_status"] == "ready"

    publish = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert publish.status_code == 200, publish.text
    assert publish.json()["parse_status"] == "published"

    # Query the knowledge base.
    query = client.post(
        "/api/v1/chat/query",
        json={
            "session_id": "e2e-session-001",
            "question": "RRC 建立失败先查告警 KPI",
            "knowledge_scopes": ["wireless"],
        },
    )
    assert query.status_code == 200, query.text
    body = query.json()
    assert "answer" in body
    assert body["citations"], "expected cited evidence in the answer"
    assert body["citations"][0]["doc_id"] == doc_id



# --------------------------------------------------------------------------- #
# RCA loop: run → review (accept) → candidate → import
# --------------------------------------------------------------------------- #


def _sample_alarms() -> list[dict]:
    return [
        {
            "alarm_id": "a_001",
            "alarm_code": "LINK_DEGRADE",
            "alarm_name": "Transmission link degradation",
            "vendor": "huawei",
            "site_id": "SITE-001",
            "cell_id": "CELL-001",
            "ne_id": "NE-001",
            "severity": "critical",
            "start_time": "2026-06-17T10:00:00+08:00",
            "raw_payload": {},
        }
    ]


def test_rca_run_review_candidate_loop() -> None:
    client = TestClient(create_rca_app(store=RcaStore()))

    # Create an RCA run.
    created = client.post(
        "/api/v1/rca/runs",
        json={
            "mode": "auto_collect",
            "max_tool_calls": 10,
            "require_human_review": True,
            "alarms": _sample_alarms(),
        },
    )
    assert created.status_code == 201, created.text
    run = created.json()
    report_id = run["report_id"]

    # Accept the report — this generates candidate knowledge.
    review = client.post(
        f"/api/v1/rca/reports/{report_id}/review",
        json={
            "decision": "accepted",
            "final_root_cause": "transmission_link_degradation",
            "reviewer": "e2e",
        },
    )
    assert review.status_code == 200

    # Candidate knowledge should now exist.
    candidates = client.get("/api/v1/candidate-knowledge")
    assert candidates.status_code == 200
    items = candidates.json()["items"]
    assert items, "expected candidate knowledge after accept"

    # Operational metrics should reflect the review.
    metrics = client.get("/api/v1/metrics/operations").json()
    assert metrics["raw"]["reviewed_reports"] >= 1
    assert metrics["raw"]["accepted_reports"] >= 1


# --------------------------------------------------------------------------- #
# Agent platform loop: run → approval → resume
# --------------------------------------------------------------------------- #


def test_agent_run_approval_resume_loop(tmp_path) -> None:
    run_store = AgentRunStore(db_path=str(tmp_path / "runs.sqlite3"))
    client = TestClient(create_platform_app(run_store=run_store))

    # RCA template requires approval.
    created = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "rca",
            "requested_by": "e2e",
            "input": {"incident_id": "inc_001"},
        },
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert created.json()["status"] == "waiting_approval"

    # Approve the pending task.
    tasks = client.get("/api/v1/approval-tasks?status=pending").json()["items"]
    task_id = next(t["task_id"] for t in tasks if t["run_id"] == run_id)
    decision = client.post(
        f"/api/v1/approval-tasks/{task_id}/decision",
        json={"decision": "approved", "decided_by": "e2e", "comment": "ok"},
    )
    assert decision.status_code == 200

    # Run should now be completed.
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run["status"] == "completed"

    # Knowledge QA template does not require approval; resume is a no-op 409.
    qa = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "knowledge_qa",
            "requested_by": "e2e",
            "input": {"question": "RRC?"},
        },
    )
    qa_run_id = qa.json()["run_id"]
    resume = client.post(f"/api/v1/agent-runs/{qa_run_id}/resume")
    assert resume.status_code == 409


# --------------------------------------------------------------------------- #
# Eval center loop: run → compare
# --------------------------------------------------------------------------- #


def test_eval_compare_loop(tmp_path) -> None:
    eval_store = EvalStore(db_path=str(tmp_path / "eval.sqlite3"))
    client = TestClient(create_platform_app(eval_store=eval_store))

    a = UnifiedReport(
        eval_type="rag", total=10, top1_coverage=0.5, top3_coverage=0.7,
        evidence_coverage=0.6, refusal_accuracy=0.8, latency_p95_ms=100.0,
    )
    b = UnifiedReport(
        eval_type="rag", total=10, top1_coverage=0.6, top3_coverage=0.75,
        evidence_coverage=0.65, refusal_accuracy=0.82, latency_p95_ms=95.0,
    )
    aid = eval_store.create_eval_run(
        eval_type="rag", template_id="knowledge_qa", golden_path="x.jsonl",
    )
    eval_store.complete_eval_run(aid, report=a.to_dict(), summary={"total": 10})
    bid = eval_store.create_eval_run(
        eval_type="rag", template_id="knowledge_qa", golden_path="x.jsonl",
    )
    eval_store.complete_eval_run(bid, report=b.to_dict(), summary={"total": 10})

    resp = client.get(f"/api/v1/evaluations/compare?run_a={aid}&run_b={bid}")
    assert resp.status_code == 200, resp.text
    by_metric = {m["metric"]: m for m in resp.json()["metrics"]}
    assert by_metric["top1_coverage"]["delta"] == pytest.approx(0.1)


# --------------------------------------------------------------------------- #
# Tool registry loop: register → list → invoke
# --------------------------------------------------------------------------- #


def test_tool_registry_register_invoke_loop(tmp_path) -> None:
    store = ToolRegistryStore(db_path=str(tmp_path / "tools.sqlite3"))
    client = TestClient(create_tools_app(store=store))

    # Built-in echo tool is present and invocable by an operator.
    listing = client.get("/api/v1/tools").json()
    assert any(t["name"] == "echo" for t in listing["tools"])

    invoke = client.post(
        "/api/v1/tools/echo/invoke",
        json={"arguments": {"text": "hello e2e"}},
        headers=_operator_headers(),
    )
    assert invoke.status_code == 200, invoke.text
    assert invoke.json()["result"] == {"echo": "hello e2e"}

    # Admin can register a new declarative tool.
    reg = client.post(
        "/api/v1/tools",
        json={
            "name": "cell.lookup",
            "description": "lookup a cell",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
            "service_name": "knowledge-api",
        },
        headers=_admin_headers(),
    )
    assert reg.status_code == 201

    # It appears in the MCP-shaped list with metadata.
    tools = client.get("/api/v1/tools").json()["tools"]
    cell = next(t for t in tools if t["name"] == "cell.lookup")
    assert cell["metadata"]["service_name"] == "knowledge-api"
    assert cell["metadata"]["risk_level"] == "read_only"


# --------------------------------------------------------------------------- #
# Inspection loop
# --------------------------------------------------------------------------- #


def test_inspection_loop(tmp_path) -> None:
    client = TestClient(create_platform_app(run_store=AgentRunStore(db_path=str(tmp_path / "r.sqlite3"))))
    resp = client.post("/api/v1/inspect/knowledge-api")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target"] == "knowledge-api"
    assert body["risk_level"] == "read_only"
    assert isinstance(body["findings"], list)
    assert (tmp_path / "inspections" / "knowledge-api.jsonl").is_file()


# --------------------------------------------------------------------------- #
# Cross-service trace: RCA metrics surfaced on the platform dashboard
# --------------------------------------------------------------------------- #


def test_rca_metrics_endpoint_shape() -> None:
    client = TestClient(create_rca_app(store=RcaStore()))
    resp = client.get("/api/v1/metrics/operations")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "tool_call_success_rate",
        "human_acceptance_rate",
        "alert_compression_ratio",
        "report_gen_seconds_avg",
        "raw",
    ):
        assert key in body
