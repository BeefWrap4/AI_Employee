from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from ai_employee.eval.golden import load_golden
from ai_employee.eval.metrics import (
    EvalResult,
    SafetyPolicyInputs,
    SafetyPolicyVerdict,
    ToolCallCorrectness,
    evaluate_safety_policy,
    evaluate_tool_call_correctness,
)

# --------------------------------------------------------------------------- #
# Eval run request (R18-1: tool_call type)
# --------------------------------------------------------------------------- #


@dataclass
class EvalRunRequest:
    """An in-process eval invocation.

    Used by the eval center and by unit tests; the CLI ``run()`` entry
    point stays a separate path that loads a golden file from disk and
    runs against a live API.
    """

    eval_type: str  # one of {"rag", "rca", "tool_call", "report", "safety"}
    template_id: str
    # RAG / RCA: empty
    # tool_call: golden (expected) + actual (run) tool names
    golden_tool_calls: list[str] = field(default_factory=list)
    actual_tool_calls: list[str] = field(default_factory=list)
    order_required: bool = False
    # report: golden report fields + actual report fields
    # safety: template + run + approval_task fields (carried via
    # ``safety_inputs``); golden/actual tool_calls may be used as a
    # convenience to populate the call list.
    safety_inputs: SafetyPolicyInputs | None = None


@dataclass
class EvalRunSummary:
    """Result of :func:`run_eval`."""

    eval_type: str
    template_id: str
    tool_call_correctness: ToolCallCorrectness | None = None
    safety_verdict: SafetyPolicyVerdict | None = None


def run_eval(req: EvalRunRequest) -> EvalRunSummary:
    """Dispatch an :class:`EvalRunRequest` to the right scorer.

    Currently supports ``tool_call`` (R18-1) and ``safety`` (R18-3).
    Other eval types (RAG, RCA) flow through :func:`run` with a
    golden file.
    """
    if req.eval_type == "tool_call":
        actual = [ToolCallCorrectness(tool_name=n, status="ok") for n in req.actual_tool_calls]
        tcc = evaluate_tool_call_correctness(
            actual=actual,
            golden=req.golden_tool_calls,
            order_required=req.order_required,
        )
        return EvalRunSummary(
            eval_type=req.eval_type,
            template_id=req.template_id,
            tool_call_correctness=tcc,
        )
    if req.eval_type == "safety":
        if req.safety_inputs is None:
            raise ValueError(
                "safety eval requires EvalRunRequest.safety_inputs to be set",
            )
        # Convenience: if the caller provided tool name lists instead
        # of a fully-populated SafetyPolicyInputs, default every tool
        # to approval_required (the most security-relevant default).
        if not req.safety_inputs.tool_calls and (req.golden_tool_calls or req.actual_tool_calls):
            req.safety_inputs.tool_calls = [
                (n, "approval_required") for n in req.actual_tool_calls
            ]
        verdict = evaluate_safety_policy(req.safety_inputs)
        return EvalRunSummary(
            eval_type=req.eval_type,
            template_id=req.template_id,
            safety_verdict=verdict,
        )
    raise NotImplementedError(
        f"run_eval does not yet support eval_type={req.eval_type!r}; "
        "use the CLI Runner for rag/rca golden-file paths.",
    )


class ApiError(Exception):
    """远程 API 调用异常（含网络错误、非 2xx 响应等）。"""


class ApiNotFound(ApiError):
    """远程 API 返回 404，表示无知识/越权（应拒答）。"""


class Api(Protocol):
    def list_documents(self) -> list[dict]: ...
    def chat_query(self, *, question: str, scopes: list[str]) -> dict: ...


class HttpApi:
    """通过 httpx 调 knowledge-api 的具体客户端。"""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def list_documents(self) -> list[dict]:
        try:
            r = httpx.get(
                f"{self.base_url}/api/v1/documents",
                params={"status": "published", "page_size": 200},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"list_documents failed: {exc}") from exc
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise ApiError(f"list_documents returned {r.status_code}: {r.text[:200]}")
        return r.json().get("items", [])

    def chat_query(self, *, question: str, scopes: list[str]) -> dict:
        try:
            r = httpx.post(
                f"{self.base_url}/api/v1/chat/query",
                json={
                    "session_id": "eval",
                    "question": question,
                    "knowledge_scopes": scopes,
                    "stream": False,
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"chat_query failed: {exc}") from exc
        if r.status_code == 404:
            raise ApiNotFound(f"chat_query 404: {r.text[:200]}")
        if r.status_code != 200:
            raise ApiError(f"chat_query returned {r.status_code}: {r.text[:200]}")
        return r.json()


def build_title_to_doc_id(docs: list[dict]) -> dict[str, str]:
    return {d["title"]: d["doc_id"] for d in docs if d.get("title") and d.get("doc_id")}


class Runner:
    def __init__(self, api: Api) -> None:
        self.api = api

    def run(self, golden_path: str, top_ks: list[int]) -> list[EvalResult]:
        items = load_golden(golden_path)
        try:
            docs = self.api.list_documents()
        except ApiError as exc:
            # 全部 errored
            return [
                EvalResult(
                    qid=it.qid,
                    question=it.question,
                    expected_doc_id=None,
                    expect_refusal=it.expect_refusal,
                    status_code=0,
                    returned_doc_ids=[],
                    answer="",
                    latency_ms=0,
                    error=str(exc),
                )
                for it in items
            ]
        title_to_id = build_title_to_doc_id(docs)

        results: list[EvalResult] = []
        for it in items:
            expected_id = title_to_id.get(it.expected_doc_title) if it.expected_doc_title else None
            t0 = time.perf_counter()
            try:
                resp = self.api.chat_query(question=it.question, scopes=it.scope)
            except ApiNotFound:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                results.append(
                    EvalResult(
                        qid=it.qid,
                        question=it.question,
                        expected_doc_id=expected_id,
                        expect_refusal=it.expect_refusal,
                        status_code=404,
                        returned_doc_ids=[],
                        answer="",
                        latency_ms=latency_ms,
                        error=None,
                    )
                )
                continue
            except ApiError as exc:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                results.append(
                    EvalResult(
                        qid=it.qid,
                        question=it.question,
                        expected_doc_id=expected_id,
                        expect_refusal=it.expect_refusal,
                        status_code=0,
                        returned_doc_ids=[],
                        answer="",
                        latency_ms=latency_ms,
                        error=str(exc),
                    )
                )
                continue
            latency_ms = int((time.perf_counter() - t0) * 1000)
            returned = [c.get("doc_id") for c in resp.get("citations", []) if c.get("doc_id")]
            results.append(
                EvalResult(
                    qid=it.qid,
                    question=it.question,
                    expected_doc_id=expected_id,
                    expect_refusal=it.expect_refusal,
                    status_code=200,
                    returned_doc_ids=returned,
                    answer=resp.get("answer", ""),
                    latency_ms=latency_ms,
                    error=None,
                )
            )
        return results


def run(
    *,
    golden_path: str,
    api_base: str,
    top_ks: list[int] = (1, 3, 5),
    timeout: float = 60.0,
) -> list[EvalResult]:
    return Runner(HttpApi(api_base, timeout)).run(golden_path, list(top_ks))


__all__ = [
    "Api",
    "ApiError",
    "ApiNotFound",
    "EvalRunRequest",
    "EvalRunSummary",
    "HttpApi",
    "Runner",
    "SafetyPolicyInputs",
    "SafetyPolicyVerdict",
    "ToolCallCorrectness",
    "build_title_to_doc_id",
    "evaluate_safety_policy",
    "evaluate_tool_call_correctness",
    "run",
    "run_eval",
]
