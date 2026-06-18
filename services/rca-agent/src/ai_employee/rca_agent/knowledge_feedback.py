"""知识回流：RCA 报告 accepted 后按假设拆分为候选知识。

Spec: Docs/superpowers/specs/2026-06-17-platform-m6-knowledge-feedback-eval-center-design.md §5
"""

from __future__ import annotations

import json
import os

import httpx
from ai_employee.rca_agent.schemas import (
    CandidateKnowledge,
    Evidence,
    Hypothesis,
    IncidentResponse,
    RcaReportResponse,
)

_TITLE_MAX = 80
DEFAULT_KNOWLEDGE_API_URL = "http://127.0.0.1:8010"


class KnowledgeApiUnavailable(Exception):
    """knowledge-api 不可达（连接/超时）。"""


class KnowledgeApiError(Exception):
    """knowledge-api 返回非 2xx 响应。"""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"knowledge-api returned {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def import_candidate_to_knowledge_api(
    candidate: CandidateKnowledge,
    api_url: str | None = None,
) -> str:
    """将 approved 候选导入 knowledge-api，返回 doc_id。

    multipart 字段见 spec §6.3。不可达抛 ``KnowledgeApiUnavailable``；
    非 2xx 抛 ``KnowledgeApiError``。
    """
    base = (api_url or os.getenv("KNOWLEDGE_API_URL") or DEFAULT_KNOWLEDGE_API_URL).rstrip("/")
    metadata = {
        "source": "rca_feedback",
        "incident_id": candidate.source_incident_id,
        "root_cause_type": candidate.root_cause_type,
    }
    files = {
        "file": (
            f"{candidate.title}.md",
            candidate.content.encode("utf-8"),
            "text/markdown",
        )
    }
    data = {
        "title": candidate.title,
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "acl_tags_json": json.dumps(["rca_feedback"]),
        "version": "v1",
        "mime_type": "text/markdown",
    }
    try:
        response = httpx.post(
            f"{base}/api/v1/documents",
            files=files,
            data=data,
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        raise KnowledgeApiUnavailable(str(exc)) from exc
    if response.status_code >= 400:
        raise KnowledgeApiError(response.status_code, response.text)
    return response.json()["doc_id"]


def generate_candidates_from_report(
    report: RcaReportResponse,
    incident: IncidentResponse,
    evidence: list[Evidence],
) -> list[CandidateKnowledge]:
    """拆分 accepted 报告为 N 条候选知识（N = 报告假设数）。

    仅当 ``report.review_status == "accepted"`` 且 ``final_root_cause`` 非空时触发；
    否则返回空列表。候选 ``candidate_id`` / ``created_at`` 在入库时由 store 分配，
    这里置空字符串占位。
    """
    if report.review_status != "accepted":
        return []
    if not report.final_root_cause:
        return []

    evidence_by_id = {item.evidence_id: item for item in evidence}
    candidates: list[CandidateKnowledge] = []
    for hypothesis in report.hypotheses:
        candidates.append(_build_candidate(report, incident, hypothesis, evidence_by_id))
    return candidates


def _build_candidate(
    report: RcaReportResponse,
    incident: IncidentResponse,
    hypothesis: Hypothesis,
    evidence_by_id: dict[str, Evidence],
) -> CandidateKnowledge:
    title = hypothesis.description[:_TITLE_MAX]
    content = _build_content(hypothesis, report.final_root_cause or "")
    evidence_summary = _build_evidence_summary(hypothesis.supporting_evidence_ids, evidence_by_id)
    return CandidateKnowledge(
        candidate_id="",
        source_report_id=report.report_id,
        source_incident_id=incident.incident_id,
        hypothesis_id=hypothesis.hypothesis_id,
        root_cause_type=hypothesis.root_cause_type,
        title=title,
        content=content,
        evidence_summary=evidence_summary,
        review_status="pending",
        created_at="",
    )


def _build_content(hypothesis: Hypothesis, final_root_cause: str) -> str:
    sections = [
        f"## 假设描述\n{hypothesis.description}",
        f"## 最终根因\n{final_root_cause}",
    ]
    remediation = hypothesis.next_check
    if remediation:
        bullets = "\n".join(f"- {step}" for step in remediation)
        sections.append(f"## 处置建议\n{bullets}")
    return "\n\n".join(sections)


def _build_evidence_summary(
    supporting_evidence_ids: list[str],
    evidence_by_id: dict[str, Evidence],
) -> str:
    lines: list[str] = []
    for eid in supporting_evidence_ids:
        item = evidence_by_id.get(eid)
        if item is None:
            continue
        lines.append(f"- [{item.source_type}] {item.content}")
    return "\n".join(lines)
