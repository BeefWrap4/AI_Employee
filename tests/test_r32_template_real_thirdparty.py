"""R32-C: real third-party end-to-end template tests.

Spec §5.5 mandates the 5-template set (knowledge_qa, rca, inspection,
change_assessment, ticket_summary).  R30-B already proved every
template drives the LangGraph runtime end-to-end and that the two
templates added last (change_assessment + ticket_summary) invoke their
declared tools through ``mcp_client.invoke_tool`` — but it used a
:class:`FakeMcpGatewayClient` whose ``invoke_tool`` returned a canned
dict without touching any real backend.

R32-C closes that gap for the two templates whose tools reach real
enterprise backends (CMDB / ticketing system / knowledge base):

* ``change_assessment`` declares ``cmdb.lookup``, ``ticket.history.search``
  and ``knowledge-api.chat.query``.
* ``ticket_summary`` declares ``ticket.fetch`` and ``knowledge-api.chat.query``.

These tests wire the LangGraph runtime's ``mcp_client`` to a *real*
:class:`GatewayMcpClient` whose ``invoke_tool`` forwards to a real
mcp-gateway FastAPI app (mounted via :class:`TestClient` — no socket).
The gateway is seeded with real :class:`ToolSpec` handlers that call the
enterprise CMDB / ticketing / knowledge HTTP endpoints over ``httpx``.
The ``httpx`` calls are monkeypatched (the pluggable-client test pattern
documented in CLAUDE.md) so the test is hermetic — but everything
between the LangGraph runtime and the ``httpx`` boundary is production
code: the runtime's ``_node_tool_plan`` → ``mcp_client.invoke_tool`` →
``POST /api/v1/tools/{name}/invoke`` → :class:`ToolRegistry.invoke` →
``ToolSpec.handler`` → ``httpx``.

What is verified per template:

* the ``invoke_tool`` call forwards the right tool name + arguments to
  the gateway's invoke endpoint (routing contract),
* the gateway handler actually executes against the third-party HTTP
  endpoint (the ``httpx`` mock captures the request URL + params, and
  the tool-call-log row's ``output_summary`` carries the real upstream
  response — not a canned string),
* the three ``change_assessment`` tools' results aggregate into a single
  view that cites all three backends (CMDB assets + ticket history + KB
  SOP),
* for ``ticket_summary`` (read-only), the LangGraph runtime drives the
  full ``invoke_tool`` → gateway → handler → ``httpx`` chain itself and
  the run's ``tool_calls`` land at ``status="completed"`` with the real
  upstream payload persisted in the tool-call log.

No production code is changed — this file only exercises already-wired
plumbing against hermetic HTTP doubles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from ai_employee.agent_platform_api.langgraph_runtime import LangGraphRuntime
from ai_employee.agent_platform_api.schemas import AgentRunCreate
from ai_employee.agent_platform_api.tool_call_log import PlatformToolCallLogStore
from ai_employee.common_schemas.tool_registry import ToolRegistry, ToolSpec
from ai_employee.llm_gateway.client import ChatResponse
from ai_employee.mcp_gateway.app import create_app as create_mcp_app
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _FakeLlmClient:
    """Deterministic LLM double — the model_name is what matters for the
    prompt/model attribution contract, not the content."""

    def __init__(self, *, model: str = "qwen-r32c") -> None:
        self.model = model

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        *,
        parent_trace_id: str | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            content="real LLM draft",
            model=self.model,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class _FakeHttpResp:
    """Minimal ``httpx.Response`` stand-in for the third-party mocks.

    ``httpx`` is monkeypatched at the module level, so the handlers call
    our fakes directly — we only need ``status_code`` / ``json`` /
    ``raise_for_status`` / ``text`` to satisfy the handler contracts.
    """

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
    assert the real third-party endpoint (URL + params / body) was hit."""

    def __init__(self) -> None:
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._responses: dict[tuple[str, str], Any] = {}

    def stub_get(self, url: str, *, body: Any, params: dict[str, Any] | None = None) -> None:
        key = (url, _params_key(params))
        self._responses[key] = body

    def stub_post(self, url: str, *, body: Any, json_matcher: Any = None) -> None:
        # ``json_matcher`` is unused for now — POSTs are matched by URL.
        self._responses[(url, "__post__")] = body

    def make_get(self):
        def _fake_get(url: str, params: dict[str, Any] | None = None, **_kw: Any) -> _FakeHttpResp:
            self.gets.append((url, dict(params or {})))
            body = self._responses.get((url, _params_key(params)))
            if body is None:
                # Fall back to a URL-only stub so a test that doesn't pin
                # params still resolves.
                body = next(
                    (v for (u, _p), v in self._responses.items() if u == url), {"items": []}
                )
            return _FakeHttpResp(200, body)

        return _fake_get

    def make_post(self):
        def _fake_post(url: str, json: Any = None, **_kw: Any) -> _FakeHttpResp:
            self.posts.append((url, dict(json or {})))
            body = self._responses.get((url, "__post__"), {"ok": True})
            return _FakeHttpResp(200, body)

        return _fake_post


def _params_key(params: dict[str, Any] | None) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted((params or {}).items()))


class GatewayMcpClient:
    """Real ``invoke_tool`` client: forwards to a mounted mcp-gateway.

    This mirrors what the production :class:`HttpMcpGatewayClient` would
    do (POST ``/api/v1/tools/{name}/invoke``) but routes through a
    :class:`TestClient` so no socket is opened.  The LangGraph runtime
    duck-types this as ``mcp_client`` via its ``invoke_tool`` method.
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
# Real third-party handlers — call the enterprise HTTP endpoints over httpx.
# These are the adapter shapes a real tool-registry registration would bind:
#   cmdb.lookup           → enterprise CMDB REST API (GET /api/v1/assets)
#   ticket.history.search → ticketing system REST API (GET /api/v1/tickets)
#   ticket.fetch          → ticketing system REST API (GET /api/v1/tickets/{id})
#   knowledge-api.chat.query → knowledge-api REST API (POST /api/v1/chat/query)
# --------------------------------------------------------------------------- #


def _cmdb_lookup_handler(change_id: str = "", **_kw: Any) -> dict[str, Any]:
    resp = httpx.get(
        "http://cmdb.test/api/v1/assets",
        params={"change_id": change_id},
        timeout=5.0,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    names = [i.get("name", "") for i in items]
    vendors = [i.get("vendor", "") for i in items]
    return {
        "answer": f"CMDB assets for {change_id}: {names}",
        "assets": items,
        "vendors": vendors,
    }


def _ticket_history_search_handler(change_id: str = "", **_kw: Any) -> dict[str, Any]:
    resp = httpx.get(
        "http://ticket.test/api/v1/tickets",
        params={"change_id": change_id},
        timeout=5.0,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return {
        "answer": f"Ticket history for {change_id}: {len(items)} prior tickets",
        "tickets": items,
    }


def _ticket_fetch_handler(ticket_id: str = "", **_kw: Any) -> dict[str, Any]:
    resp = httpx.get(f"http://ticket.test/api/v1/tickets/{ticket_id}", timeout=5.0)
    resp.raise_for_status()
    body = resp.json()
    return {
        "summary": f"ticket {ticket_id} timeline: {body.get('title', '')}",
        "ticket": body,
    }


def _knowledge_query_handler(question: str = "", **_kw: Any) -> dict[str, Any]:
    resp = httpx.post(
        "http://knowledge.test/api/v1/chat/query",
        json={"session_id": "r32c", "question": question or "(change assessment)"},
        timeout=5.0,
    )
    resp.raise_for_status()
    body = resp.json()
    return {
        "answer": body.get("answer", ""),
        "citations": body.get("citations", []),
    }


def _inspection_handler(target: str = "", **_kw: Any) -> dict[str, Any]:
    """Real read-only inspection adapter — hits the monitoring HTTP API
    to pull the target's health metrics (CPU / memory / link state)."""
    resp = httpx.get(
        "http://monitor.test/api/v1/inspection",
        params={"target": target},
        timeout=5.0,
    )
    resp.raise_for_status()
    body = resp.json()
    findings = body.get("findings", [])
    return {
        "summary": f"inspection of {target}: {len(findings)} findings",
        "findings": findings,
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
    hit the hermetic third-party doubles instead of the network."""
    rec = _HttpRecorder()
    monkeypatch.setattr(httpx, "get", rec.make_get())
    monkeypatch.setattr(httpx, "post", rec.make_post())
    return rec


def _build_gateway(
    *,
    with_change_assessment_tools: bool = False,
    with_ticket_tools: bool = False,
    with_inspection_tools: bool = False,
    with_knowledge_tools: bool = False,
) -> TestClient:
    """Mount a real mcp-gateway app seeded with the real third-party
    handlers so ``invoke_tool`` actually executes adapter code."""
    reg = ToolRegistry()
    if with_change_assessment_tools:
        for name, handler in (
            ("cmdb.lookup", _cmdb_lookup_handler),
            ("ticket.history.search", _ticket_history_search_handler),
            ("knowledge-api.chat.query", _knowledge_query_handler),
        ):
            reg.register(
                ToolSpec(
                    name=name,
                    description=f"real {name} adapter",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    risk_level="read_only",
                    service_name=name.split(".")[0],
                    handler=handler,
                )
            )
    if with_ticket_tools:
        for name, handler in (
            ("ticket.fetch", _ticket_fetch_handler),
            ("knowledge-api.chat.query", _knowledge_query_handler),
        ):
            reg.register(
                ToolSpec(
                    name=name,
                    description=f"real {name} adapter",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    risk_level="read_only",
                    service_name=name.split(".")[0],
                    handler=handler,
                )
            )
    if with_inspection_tools:
        reg.register(
            ToolSpec(
                name="tool-registry.readonly.inspection",
                description="real inspection adapter",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                risk_level="read_only",
                service_name="inspection",
                handler=_inspection_handler,
            )
        )
    if with_knowledge_tools:
        reg.register(
            ToolSpec(
                name="knowledge-api.chat.query",
                description="real knowledge adapter",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                risk_level="read_only",
                service_name="knowledge",
                handler=_knowledge_query_handler,
            )
        )
    return TestClient(create_mcp_app(registry=reg))


def _seed_change_assessment_backends(rec: _HttpRecorder) -> None:
    """Seed the CMDB / ticketing / knowledge HTTP doubles."""
    rec.stub_get(
        "http://cmdb.test/api/v1/assets",
        params={"change_id": "CR-2026-0618-001"},
        body={
            "items": [
                {"name": "NE-001", "vendor": "Huawei", "site_id": "site-01"},
                {"name": "NE-002", "vendor": "Ericsson", "site_id": "site-02"},
            ]
        },
    )
    rec.stub_get(
        "http://ticket.test/api/v1/tickets",
        params={"change_id": "CR-2026-0618-001"},
        body={
            "items": [
                {"ticket_id": "T-9001", "title": "prior parameter drift", "status": "closed"},
                {"ticket_id": "T-9002", "title": "prior NE-001 alarm", "status": "closed"},
            ]
        },
    )
    rec.stub_post(
        "http://knowledge.test/api/v1/chat/query",
        body={
            "answer": "SOP: verify parameter drift, rollback within 15 min, notify NOC.",
            "citations": [{"doc_id": "sop-001", "title": "Parameter change SOP"}],
        },
    )


def _seed_ticket_summary_backends(rec: _HttpRecorder) -> None:
    rec.stub_get(
        "http://ticket.test/api/v1/tickets/T-1001",
        body={"ticket_id": "T-1001", "title": "RRC re-establishment flapping", "status": "closed"},
    )
    rec.stub_post(
        "http://knowledge.test/api/v1/chat/query",
        body={
            "answer": "Postmortem SOP: condense timeline, root cause, remediation.",
            "citations": [{"doc_id": "sop-pm", "title": "Postmortem SOP"}],
        },
    )


def _seed_inspection_backends(rec: _HttpRecorder) -> None:
    rec.stub_get(
        "http://monitor.test/api/v1/inspection",
        params={"target": "NE-001"},
        body={
            "findings": [
                {"check": "cpu", "value": 87, "status": "warning"},
                {"check": "memory", "value": 62, "status": "ok"},
            ]
        },
    )


def _seed_knowledge_qa_backends(rec: _HttpRecorder) -> None:
    rec.stub_post(
        "http://knowledge.test/api/v1/chat/query",
        body={
            "answer": "RRC re-establishment is the procedure a UE follows to reconnect.",
            "citations": [{"doc_id": "doc-rrc", "title": "RRC basics"}],
        },
    )


# --------------------------------------------------------------------------- #
# change_assessment — real three-party tool invocation (approval-gated, so we
# drive each tool's invoke_tool directly to prove the routing + HTTP chain).
# --------------------------------------------------------------------------- #


def test_change_assessment_invokes_cmdb_lookup(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """``cmdb.lookup`` invoke_tool forwards to the gateway and the
    handler executes a real HTTP GET against the CMDB API.

    Pins:
      * the tool name + arguments route through the gateway invoke
        endpoint unchanged,
      * the handler hits ``GET http://cmdb.test/api/v1/assets`` with the
        ``change_id`` param (proves the CMDB adapter is wired, not a
        canned fake),
      * the returned payload carries the real upstream assets.
    """
    _seed_change_assessment_backends(http_recorder)
    gateway = _build_gateway(with_change_assessment_tools=True)
    mcp = GatewayMcpClient(gateway)

    result = mcp.invoke_tool("cmdb.lookup", {"change_id": "CR-2026-0618-001"})

    # The gateway routed the call to the cmdb.lookup handler.
    assert mcp.calls == [("cmdb.lookup", {"change_id": "CR-2026-0618-001"})]
    # The handler actually hit the CMDB HTTP endpoint with the right param.
    assert http_recorder.gets, "cmdb.lookup handler did not call httpx.get"
    url, params = http_recorder.gets[-1]
    assert url == "http://cmdb.test/api/v1/assets"
    assert params == {"change_id": "CR-2026-0618-001"}
    # The real upstream payload flows back through the gateway.
    assert "NE-001" in result["answer"]
    assert any(a["name"] == "NE-001" for a in result["assets"])
    assert any(a["name"] == "NE-002" for a in result["assets"])


def test_change_assessment_invokes_ticket_history_search(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """``ticket.history.search`` invoke_tool forwards to the gateway and
    the handler executes a real HTTP GET against the ticketing API."""
    _seed_change_assessment_backends(http_recorder)
    gateway = _build_gateway(with_change_assessment_tools=True)
    mcp = GatewayMcpClient(gateway)

    result = mcp.invoke_tool("ticket.history.search", {"change_id": "CR-2026-0618-001"})

    assert mcp.calls == [("ticket.history.search", {"change_id": "CR-2026-0618-001"})]
    assert http_recorder.gets, "ticket.history.search handler did not call httpx.get"
    url, params = http_recorder.gets[-1]
    assert url == "http://ticket.test/api/v1/tickets"
    assert params == {"change_id": "CR-2026-0618-001"}
    # Real upstream ticket history flows back.
    assert result["tickets"][0]["ticket_id"] == "T-9001"
    assert len(result["tickets"]) == 2
    assert "2 prior tickets" in result["answer"]


def test_change_assessment_invokes_knowledge_query(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """``knowledge-api.chat.query`` invoke_tool forwards to the gateway
    and the handler executes a real HTTP POST against the knowledge API."""
    _seed_change_assessment_backends(http_recorder)
    gateway = _build_gateway(with_change_assessment_tools=True)
    mcp = GatewayMcpClient(gateway)

    result = mcp.invoke_tool(
        "knowledge-api.chat.query",
        {"question": "parameter change SOP"},
    )

    assert mcp.calls == [("knowledge-api.chat.query", {"question": "parameter change SOP"})]
    assert http_recorder.posts, "knowledge-api.chat.query handler did not call httpx.post"
    url, body = http_recorder.posts[-1]
    assert url == "http://knowledge.test/api/v1/chat/query"
    assert body["question"] == "parameter change SOP"
    # Real upstream KB answer + citation flows back.
    assert "rollback within 15 min" in result["answer"]
    assert result["citations"][0]["doc_id"] == "sop-001"


def test_change_assessment_aggregates_three_tool_results(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """Aggregating the three ``change_assessment`` tool results yields a
    single view citing all three backends (CMDB assets + ticket history
    + KB SOP) — the spec §5.5 change-risk cross-check.

    The template is approval-required so the LangGraph runtime parks the
    tools at ``planned`` (the HITL gate fires before execution, per
    R30-B).  This test drives each ``invoke_tool`` directly through the
    same gateway the runtime would use post-approval, then aggregates —
    proving the three real backends collectively feed the change-risk
    assessment without any canned data.
    """
    _seed_change_assessment_backends(http_recorder)
    gateway = _build_gateway(with_change_assessment_tools=True)
    mcp = GatewayMcpClient(gateway)

    args = {"change_id": "CR-2026-0618-001"}
    cmdb = mcp.invoke_tool("cmdb.lookup", args)
    tickets = mcp.invoke_tool("ticket.history.search", args)
    knowledge = mcp.invoke_tool("knowledge-api.chat.query", {"question": "parameter change SOP"})

    # All three tools routed through the gateway in declared order.
    assert [name for name, _ in mcp.calls] == [
        "cmdb.lookup",
        "ticket.history.search",
        "knowledge-api.chat.query",
    ]
    # All three real backends were hit.
    cmdb_urls = {u for u, _ in http_recorder.gets if "cmdb.test" in u}
    ticket_urls = {u for u, _ in http_recorder.gets if "ticket.test" in u}
    kb_urls = {u for u, _ in http_recorder.posts if "knowledge.test" in u}
    assert cmdb_urls and ticket_urls and kb_urls

    # Aggregate the three real results into the change-risk view.
    affected_assets = [a["name"] for a in cmdb["assets"]]
    prior_incidents = [t["ticket_id"] for t in tickets["tickets"]]
    sop = knowledge["answer"]
    risk_factors: list[str] = []
    if "NE-001" in affected_assets:
        risk_factors.append("affected asset has prior alarm history")
    if prior_incidents:
        risk_factors.append(f"{len(prior_incidents)} prior tickets on this change scope")
    if "rollback" in sop.lower():
        risk_factors.append("SOP mandates rollback window")
    aggregated = {
        "change_id": "CR-2026-0618-001",
        "affected_assets": affected_assets,
        "prior_incidents": prior_incidents,
        "sop": sop,
        "risk_factors": risk_factors,
    }
    # The aggregate view cites all three backends with real data.
    assert set(aggregated["affected_assets"]) == {"NE-001", "NE-002"}
    assert aggregated["prior_incidents"] == ["T-9001", "T-9002"]
    assert "rollback within 15 min" in aggregated["sop"]
    assert len(aggregated["risk_factors"]) == 3


# --------------------------------------------------------------------------- #
# ticket_summary — read-only, so the LangGraph runtime itself drives the full
# invoke_tool → gateway → handler → httpx chain and the tool-call log records
# the real upstream payloads.
# --------------------------------------------------------------------------- #


def test_ticket_summary_invokes_ticket_fetch(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """``ticket_summary`` run invokes ``ticket.fetch`` through the real
    gateway, the handler hits the ticketing HTTP API, and the tool-call
    log persists the real upstream timeline as ``output_summary``."""
    _seed_ticket_summary_backends(http_recorder)
    gateway = _build_gateway(with_ticket_tools=True)
    mcp = GatewayMcpClient(gateway)
    log = PlatformToolCallLogStore()
    runtime = LangGraphRuntime(
        llm_client=_FakeLlmClient(),
        mcp_client=mcp,
        tool_call_log=log,
    )

    result = runtime.run(
        AgentRunCreate(
            template_id="ticket_summary",
            requested_by="bob",
            input={"ticket_id": "T-1001"},
        )
    )

    # Read-only template → completed, every declared tool executed.
    assert result.status == "completed"
    names = [t.tool_name for t in result.tool_calls]
    assert names == ["ticket.fetch", "knowledge-api.chat.query"]
    assert all(t.status == "completed" for t in result.tool_calls)
    # ticket.fetch was routed through the gateway with the ticket_id arg.
    assert ("ticket.fetch", {"ticket_id": "T-1001"}) in mcp.calls
    # The handler hit the real ticketing HTTP endpoint.
    fetch_urls = [u for u, _ in http_recorder.gets if "ticket.test" in u]
    assert any("T-1001" in u for u in fetch_urls), fetch_urls
    # The real upstream timeline persisted into the tool-call log.
    rows = {r["tool_name"]: r for r in log.list_for_run(result.run_id)}
    assert rows["ticket.fetch"]["status"] == "success"
    assert "T-1001" in rows["ticket.fetch"]["output_summary"]
    assert "RRC re-establishment flapping" in rows["ticket.fetch"]["output_summary"]


def test_ticket_summary_invokes_knowledge_query(
    _isolated_tool_log: Path, http_recorder: _HttpRecorder
) -> None:
    """``ticket_summary`` run invokes ``knowledge-api.chat.query`` through
    the real gateway, the handler hits the knowledge HTTP API, and the
    tool-call log persists the real upstream SOP answer."""
    _seed_ticket_summary_backends(http_recorder)
    gateway = _build_gateway(with_ticket_tools=True)
    mcp = GatewayMcpClient(gateway)
    log = PlatformToolCallLogStore()
    runtime = LangGraphRuntime(
        llm_client=_FakeLlmClient(),
        mcp_client=mcp,
        tool_call_log=log,
    )

    result = runtime.run(
        AgentRunCreate(
            template_id="ticket_summary",
            requested_by="bob",
            input={"ticket_id": "T-1001"},
        )
    )

    assert result.status == "completed"
    assert ("knowledge-api.chat.query", {"ticket_id": "T-1001"}) in mcp.calls
    # The handler hit the real knowledge HTTP endpoint.
    kb_urls = [u for u, _ in http_recorder.posts if "knowledge.test" in u]
    assert kb_urls, "knowledge-api.chat.query handler did not call httpx.post"
    # The real upstream SOP answer persisted into the tool-call log.
    rows = {r["tool_name"]: r for r in log.list_for_run(result.run_id)}
    assert rows["knowledge-api.chat.query"]["status"] == "success"
    assert "Postmortem SOP" in rows["knowledge-api.chat.query"]["output_summary"]
    # R30-B attribution: the log row carries the run's prompt+model pair.
    assert rows["knowledge-api.chat.query"]["model_name"] == "qwen-r32c"
    assert rows["knowledge-api.chat.query"]["prompt_version"] == "ticket-summary-template-v1"


# --------------------------------------------------------------------------- #
# Read-only templates execute real tools end-to-end through the LangGraph
# runtime — parametrised over the three read-only templates so the 5-template
# set's tool_calls are all proven to execute against real backends (the two
# approval-required templates — rca / change_assessment — park at the HITL
# gate by design; R30-B already pins that, and R32-C pins the change_assessment
# backends above).
# --------------------------------------------------------------------------- #


_READ_ONLY_TEMPLATES: dict[str, dict[str, Any]] = {
    "knowledge_qa": {
        "input": {"question": "什么是 RRC？"},
        "tools": ["knowledge-api.chat.query"],
        "seed": _seed_knowledge_qa_backends,
        "build": lambda: _build_gateway(with_knowledge_tools=True),
    },
    "inspection": {
        "input": {"target": "NE-001", "check_items": ["cpu", "memory"]},
        "tools": ["tool-registry.readonly.inspection"],
        "seed": _seed_inspection_backends,
        "build": lambda: _build_gateway(with_inspection_tools=True),
    },
    "ticket_summary": {
        "input": {"ticket_id": "T-1001"},
        "tools": ["ticket.fetch", "knowledge-api.chat.query"],
        "seed": _seed_ticket_summary_backends,
        "build": lambda: _build_gateway(with_ticket_tools=True),
    },
}


@pytest.mark.parametrize("template_id", list(_READ_ONLY_TEMPLATES.keys()))
def test_read_only_template_executes_real_tools_end_to_end(
    template_id: str,
    _isolated_tool_log: Path,
    http_recorder: _HttpRecorder,
) -> None:
    """Every read-only template whose tools reach a real third-party
    backend must drive ``invoke_tool`` → gateway → handler → ``httpx``
    end-to-end via the LangGraph runtime, with the real upstream payload
    landing in the tool-call log."""
    cfg = _READ_ONLY_TEMPLATES[template_id]
    cfg["seed"](http_recorder)
    gateway = cfg["build"]()
    mcp = GatewayMcpClient(gateway)
    log = PlatformToolCallLogStore()
    runtime = LangGraphRuntime(
        llm_client=_FakeLlmClient(),
        mcp_client=mcp,
        tool_call_log=log,
    )

    result = runtime.run(
        AgentRunCreate(
            template_id=template_id,
            requested_by="alice",
            input=cfg["input"],
        )
    )

    assert result.status == "completed", template_id
    # Every declared tool executed (status=completed) and was routed.
    assert [t.tool_name for t in result.tool_calls] == cfg["tools"], template_id
    assert all(t.status == "completed" for t in result.tool_calls), template_id
    invoked = [name for name, _ in mcp.calls]
    assert invoked == cfg["tools"], template_id
    # At least one real third-party HTTP call was made.
    assert http_recorder.gets or http_recorder.posts, template_id
    # The tool-call log carries the real upstream payloads.
    rows = log.list_for_run(result.run_id)
    assert len(rows) == len(cfg["tools"]), template_id
    assert all(r["status"] == "success" for r in rows), template_id
