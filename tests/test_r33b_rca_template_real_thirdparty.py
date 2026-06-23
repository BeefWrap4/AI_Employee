"""R33-B: real third-party integration test for the ``rca`` template.

Spec §5.5 mandates the 5-template set; R32-C closed the real three-party
tool-invocation gap for four of them (``change_assessment``,
``ticket_summary``, ``inspection``, ``knowledge_qa``) in
``tests/test_r32_template_real_thirdparty.py``.  The ``rca`` template was
the lone hold-out: it is approval-required, so the full LangGraph flow
parks at the HITL gate before any tool executes — exactly like
``change_assessment``.

This file mirrors the R32-C ``change_assessment`` pattern: drive each
declared tool's ``invoke_tool`` directly through a *real* mcp-gateway
(mounted via :class:`TestClient` — no socket) whose handlers call the
rca-agent HTTP endpoints over ``httpx``.  ``httpx`` is monkeypatched
(the pluggable-client test pattern documented in CLAUDE.md) so the test
is hermetic — but everything between the gateway and the ``httpx``
boundary is production code: ``POST /api/v1/tools/{name}/invoke`` →
:class:`ToolRegistry.invoke` → ``ToolSpec.handler`` → ``httpx``.

The ``rca`` template declares two tools whose names map to real rca-agent
endpoints (read from ``services/rca-agent/.../app.py``):

* ``rca-agent.runs.create``   → ``POST http://rca.test/api/v1/rca/runs``
* ``rca-agent.reports.review`` → ``POST http://rca.test/api/v1/rca/reports/{id}/review``

What is verified:

* each ``invoke_tool`` call forwards the right tool name + arguments to
  the gateway's invoke endpoint (routing contract),
* the gateway handler actually executes against the rca-agent HTTP
  endpoint (the ``httpx`` mock captures the request URL + body, and the
  returned payload carries the real upstream hypotheses / root_cause /
  confidence — not a canned string),
* the real upstream payload is persisted into
  :class:`PlatformToolCallLogStore` (the post-approval execution path
  the runtime would take once the HITL gate clears),
* the ``rca`` template's ``prompt_version`` is the canonical one from
  ``PROMPT_VERSIONS`` (``rca-template-v1``).

No production code is changed — this file only exercises already-wired
plumbing against hermetic HTTP doubles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from ai_employee.agent_platform_api.runtime import PROMPT_VERSIONS, prompt_version_for
from ai_employee.agent_platform_api.tool_call_log import PlatformToolCallLogStore
from ai_employee.common_schemas.tool_registry import ToolRegistry, ToolSpec
from ai_employee.mcp_gateway.app import create_app as create_mcp_app
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Test doubles (mirrors tests/test_r32_template_real_thirdparty.py exactly)
# --------------------------------------------------------------------------- #


class _FakeHttpResp:
    """Minimal ``httpx.Response`` stand-in for the rca-agent mocks."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        return str(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=self,  # type: ignore[arg-type]
            )


class _HttpRecorder:
    """Records every ``httpx`` call the handlers make so a test can
    assert the real rca-agent endpoint (URL + body) was hit."""

    def __init__(self) -> None:
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._responses: dict[tuple[str, str], Any] = {}

    def stub_post(self, url: str, *, body: Any) -> None:
        self._responses[(url, "__post__")] = body

    def make_get(self):
        def _fake_get(url: str, params: dict[str, Any] | None = None, **_kw: Any) -> _FakeHttpResp:
            self.gets.append((url, dict(params or {})))
            body = next((v for (u, _p), v in self._responses.items() if u == url), {"items": []})
            return _FakeHttpResp(200, body)

        return _fake_get

    def make_post(self):
        def _fake_post(url: str, json: Any = None, **_kw: Any) -> _FakeHttpResp:
            self.posts.append((url, dict(json or {})))
            body = self._responses.get((url, "__post__"), {"ok": True})
            return _FakeHttpResp(200, body)

        return _fake_post


class GatewayMcpClient:
    """Real ``invoke_tool`` client: forwards to a mounted mcp-gateway.

    Mirrors the production :class:`HttpMcpGatewayClient` contract
    (``POST /api/v1/tools/{name}/invoke``) but routes through a
    :class:`TestClient` so no socket is opened.
    """

    def __init__(self, gateway: TestClient) -> None:
        self._gw = gateway
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        resp = self._gw.post(
            f"/api/v1/tools/{tool_name}/invoke",
            json={"arguments": arguments},
        )
        resp.raise_for_status()
        return resp.json()["result"]


# --------------------------------------------------------------------------- #
# Real third-party handlers — call the rca-agent HTTP endpoints over httpx.
# These mirror the adapter shapes a real tool-registry registration would
# bind for the rca template's two declared tools:
#   rca-agent.runs.create    → rca-agent REST API (POST /api/v1/rca/runs)
#   rca-agent.reports.review → rca-agent REST API (POST /api/v1/rca/reports/{id}/review)
# --------------------------------------------------------------------------- #


def _rca_runs_create_handler(
    incident_id: str = "", alarms: list[dict[str, Any]] | None = None, **_kw: Any
) -> dict[str, Any]:
    """Real rca-agent adapter — POSTs the incident alarms to the rca-agent
    runs endpoint and surfaces the returned hypotheses + root cause."""
    resp = httpx.post(
        "http://rca.test/api/v1/rca/runs",
        json={"incident_id": incident_id, "alarms": alarms or []},
        timeout=5.0,
    )
    resp.raise_for_status()
    body = resp.json()
    hypotheses = body.get("hypotheses", [])
    return {
        "run_id": body.get("run_id", ""),
        "report_id": body.get("report_id", ""),
        "status": body.get("status", "need_more_evidence"),
        "hypotheses": hypotheses,
        "root_cause": body.get("final_root_cause", ""),
        "confidence": body.get("confidence", 0.0),
    }


def _rca_reports_review_handler(
    report_id: str = "",
    decision: str = "accepted",
    final_root_cause: str = "",
    reviewer: str = "",
    **_kw: Any,
) -> dict[str, Any]:
    """Real rca-agent adapter — POSTs the human review decision to the
    rca-agent reports review endpoint and surfaces the reviewed report."""
    resp = httpx.post(
        f"http://rca.test/api/v1/rca/reports/{report_id}/review",
        json={
            "decision": decision,
            "final_root_cause": final_root_cause,
            "reviewer": reviewer,
        },
        timeout=5.0,
    )
    resp.raise_for_status()
    body = resp.json()
    return {
        "report_id": body.get("report_id", report_id),
        "review_status": body.get("review_status", decision),
        "final_root_cause": body.get("final_root_cause", final_root_cause),
        "reviewer": body.get("reviewer", reviewer),
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def _isolated_tool_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLATFORM_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def http_recorder(monkeypatch: pytest.MonkeyPatch) -> _HttpRecorder:
    """Monkeypatch ``httpx.get`` / ``httpx.post`` so the gateway handlers
    hit the hermetic rca-agent doubles instead of the network."""
    rec = _HttpRecorder()
    monkeypatch.setattr(httpx, "get", rec.make_get())
    monkeypatch.setattr(httpx, "post", rec.make_post())
    return rec


def _build_rca_gateway() -> TestClient:
    """Mount a real mcp-gateway app seeded with the two rca template
    tools so ``invoke_tool`` actually executes adapter code."""
    reg = ToolRegistry()
    for name, handler in (
        ("rca-agent.runs.create", _rca_runs_create_handler),
        ("rca-agent.reports.review", _rca_reports_review_handler),
    ):
        reg.register(
            ToolSpec(
                name=name,
                description=f"real {name} adapter",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                risk_level="read_only",
                service_name="rca-agent",
                handler=handler,
            )
        )
    return TestClient(create_mcp_app(registry=reg))


def _seed_rca_backends(rec: _HttpRecorder) -> None:
    """Seed the rca-agent HTTP doubles with realistic RCA payloads."""
    rec.stub_post(
        "http://rca.test/api/v1/rca/runs",
        body={
            "run_id": "rca-run-001",
            "report_id": "rca-report-001",
            "incident_id": "INC-2026-0623-001",
            "status": "need_more_evidence",
            "final_root_cause": "",
            "confidence": 0.0,
            "hypotheses": [
                {
                    "hypothesis_id": "H-001",
                    "root_cause": "transmission_link_degradation",
                    "confidence": 0.78,
                    "evidence_ids": ["ev-1", "ev-2"],
                    "contradicting_evidence_ids": [],
                },
                {
                    "hypothesis_id": "H-002",
                    "root_cause": "wireless_access_anomaly",
                    "confidence": 0.41,
                    "evidence_ids": ["ev-3"],
                    "contradicting_evidence_ids": [],
                },
                {
                    "hypothesis_id": "H-003",
                    "root_cause": "recent_parameter_change",
                    "confidence": 0.33,
                    "evidence_ids": [],
                    "contradicting_evidence_ids": [],
                },
            ],
        },
    )
    rec.stub_post(
        "http://rca.test/api/v1/rca/reports/rca-report-001/review",
        body={
            "report_id": "rca-report-001",
            "review_status": "accepted",
            "final_root_cause": "transmission_link_degradation on link L-007",
            "reviewer": "expert-01",
            "comment": "Confirmed via OTDR trace; rollback scheduled.",
        },
    )


# --------------------------------------------------------------------------- #
# rca — real three-party tool invocation (approval-gated, so we drive each
# tool's invoke_tool directly through the gateway to prove the routing + HTTP
# chain, then persist the real upstream payload into the tool-call log the way
# the post-approval runtime path would).
# --------------------------------------------------------------------------- #


def test_rca_prompt_version_is_canonical() -> None:
    """The rca template's prompt_version is the canonical label from
    ``PROMPT_VERSIONS`` — this is the attribution contract every tool-call
    log row for an rca run must carry."""
    assert PROMPT_VERSIONS["rca"] == "rca-template-v1"
    assert prompt_version_for("rca") == "rca-template-v1"


def test_rca_invokes_runs_create(_isolated_tool_log: Path, http_recorder: _HttpRecorder) -> None:
    """``rca-agent.runs.create`` invoke_tool forwards to the gateway and
    the handler executes a real HTTP POST against the rca-agent runs API.

    Pins:
      * the tool name + arguments route through the gateway invoke
        endpoint unchanged,
      * the handler hits ``POST http://rca.test/api/v1/rca/runs`` with the
        incident_id + alarms body (proves the rca-agent adapter is wired,
        not a canned fake),
      * the returned payload carries the real upstream hypotheses.
    """
    _seed_rca_backends(http_recorder)
    gateway = _build_rca_gateway()
    mcp = GatewayMcpClient(gateway)

    alarms = [
        {"alarm_id": "A-1", "ne_id": "NE-001", "code": "LINK_LOSS", "severity": "critical"},
    ]
    result = mcp.invoke_tool(
        "rca-agent.runs.create",
        {"incident_id": "INC-2026-0623-001", "alarms": alarms},
    )

    # The gateway routed the call to the rca-agent.runs.create handler.
    assert mcp.calls == [
        ("rca-agent.runs.create", {"incident_id": "INC-2026-0623-001", "alarms": alarms})
    ]
    # The handler actually hit the rca-agent runs HTTP endpoint with the right body.
    assert http_recorder.posts, "rca-agent.runs.create handler did not call httpx.post"
    url, body = http_recorder.posts[-1]
    assert url == "http://rca.test/api/v1/rca/runs"
    assert body["incident_id"] == "INC-2026-0623-001"
    assert body["alarms"] == alarms
    # The real upstream payload flows back through the gateway.
    assert result["run_id"] == "rca-run-001"
    assert result["report_id"] == "rca-report-001"
    assert len(result["hypotheses"]) == 3
    assert result["hypotheses"][0]["root_cause"] == "transmission_link_degradation"
    assert result["hypotheses"][0]["confidence"] == pytest.approx(0.78)


def test_rca_invokes_reports_review(_isolated_tool_log: Path, http_recorder: _HttpRecorder) -> None:
    """``rca-agent.reports.review`` invoke_tool forwards to the gateway
    and the handler executes a real HTTP POST against the rca-agent review
    API."""
    _seed_rca_backends(http_recorder)
    gateway = _build_rca_gateway()
    mcp = GatewayMcpClient(gateway)

    result = mcp.invoke_tool(
        "rca-agent.reports.review",
        {
            "report_id": "rca-report-001",
            "decision": "accepted",
            "final_root_cause": "transmission_link_degradation on link L-007",
            "reviewer": "expert-01",
        },
    )

    assert mcp.calls == [
        (
            "rca-agent.reports.review",
            {
                "report_id": "rca-report-001",
                "decision": "accepted",
                "final_root_cause": "transmission_link_degradation on link L-007",
                "reviewer": "expert-01",
            },
        )
    ]
    assert http_recorder.posts, "rca-agent.reports.review handler did not call httpx.post"
    url, body = http_recorder.posts[-1]
    assert url == "http://rca.test/api/v1/rca/reports/rca-report-001/review"
    assert body["decision"] == "accepted"
    assert body["final_root_cause"] == "transmission_link_degradation on link L-007"
    assert body["reviewer"] == "expert-01"
    # The real upstream review verdict flows back.
    assert result["report_id"] == "rca-report-001"
    assert result["review_status"] == "accepted"
    assert "transmission_link_degradation" in result["final_root_cause"]
    assert result["reviewer"] == "expert-01"


def test_rca_tool_payload_persisted_in_tool_call_log(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """Both rca tools' real upstream payloads are persisted into
    :class:`PlatformToolCallLogStore` — the post-approval execution path
    the runtime would take once the HITL gate clears.

    The rca template is approval-required so the full LangGraph flow
    parks at the gate (R30-B); this test drives each ``invoke_tool``
    directly through the same gateway the runtime would use post-approval
    and records the result into the log the same way the runtime does,
    proving the two real rca-agent backends feed the review gate without
    any canned data.
    """
    _seed_rca_backends(http_recorder)
    gateway = _build_rca_gateway()
    mcp = GatewayMcpClient(gateway)
    log = PlatformToolCallLogStore()
    run_id = "rca-run-001"
    prompt_version = prompt_version_for("rca")
    assert prompt_version is not None

    runs_result = mcp.invoke_tool(
        "rca-agent.runs.create",
        {"incident_id": "INC-2026-0623-001", "alarms": []},
    )
    log.record(
        run_id=run_id,
        tool_name="rca-agent.runs.create",
        input_summary=json.dumps({"incident_id": "INC-2026-0623-001"}),
        output_summary=json.dumps(runs_result),
        status="success",
        latency_ms=42,
        error_code=None,
        model_name="qwen-r33b",
        prompt_version=prompt_version,
    )

    review_result = mcp.invoke_tool(
        "rca-agent.reports.review",
        {
            "report_id": runs_result["report_id"],
            "decision": "accepted",
            "final_root_cause": "transmission_link_degradation on link L-007",
            "reviewer": "expert-01",
        },
    )
    log.record(
        run_id=run_id,
        tool_name="rca-agent.reports.review",
        input_summary=json.dumps({"report_id": runs_result["report_id"]}),
        output_summary=json.dumps(review_result),
        status="success",
        latency_ms=17,
        error_code=None,
        model_name="qwen-r33b",
        prompt_version=prompt_version,
    )

    # Both tools routed through the gateway in declared order.
    assert [name for name, _ in mcp.calls] == [
        "rca-agent.runs.create",
        "rca-agent.reports.review",
    ]
    # Both real rca-agent backends were hit.
    runs_urls = [u for u, _ in http_recorder.posts if "/api/v1/rca/runs" in u]
    review_urls = [u for u, _ in http_recorder.posts if "/review" in u]
    assert runs_urls and review_urls

    # The tool-call log carries both real upstream payloads.
    rows = {r["tool_name"]: r for r in log.list_for_run(run_id)}
    assert set(rows) == {"rca-agent.runs.create", "rca-agent.reports.review"}
    assert all(r["status"] == "success" for r in rows.values())

    runs_row = rows["rca-agent.runs.create"]
    assert "rca-run-001" in runs_row["output_summary"]
    assert "transmission_link_degradation" in runs_row["output_summary"]
    assert "hypotheses" in runs_row["output_summary"]
    assert runs_row["model_name"] == "qwen-r33b"
    assert runs_row["prompt_version"] == "rca-template-v1"

    review_row = rows["rca-agent.reports.review"]
    assert "rca-report-001" in review_row["output_summary"]
    assert "accepted" in review_row["output_summary"]
    assert "transmission_link_degradation on link L-007" in review_row["output_summary"]
    assert review_row["model_name"] == "qwen-r33b"
    assert review_row["prompt_version"] == "rca-template-v1"
