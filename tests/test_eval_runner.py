import json
from pathlib import Path

import pytest

from ai_employee.eval.golden import load_golden
from ai_employee.eval.metrics import EvalResult
from ai_employee.eval.runner import (
    ApiError,
    ApiNotFound,
    HttpApi,
    Runner,
    build_title_to_doc_id,
    run,
)


def _write_golden(tmp_path: Path, lines: list[dict]) -> Path:
    p = tmp_path / "g.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return p


def _ok(citations_doc_ids: list[str]) -> dict:
    return {
        "answer": "A",
        "citations": [{"doc_id": d, "doc_title": d, "page_no": 1, "section_path": "root"} for d in citations_doc_ids],
        "confidence": 0.9,
        "trace_id": "t",
    }


def test_build_title_to_doc_id_uses_title_field() -> None:
    docs = [
        {"doc_id": "doc_001", "title": "无线 SOP", "parse_status": "published"},
        {"doc_id": "doc_002", "title": "传输 SOP", "parse_status": "published"},
    ]
    m = build_title_to_doc_id(docs)
    assert m == {"无线 SOP": "doc_001", "传输 SOP": "doc_002"}


def test_runner_routes_via_documents_then_chat_query(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [
        {"qid": "q01", "question": "Q1", "expected_doc_title": "无线 SOP",
         "scope": ["s"], "expect_refusal": False, "tags": []},
    ])

    class FakeApi:
        def list_documents(self):
            return [
                {"doc_id": "doc_001", "title": "无线 SOP", "parse_status": "published"},
            ]

        def chat_query(self, *, question, scopes):
            assert question == "Q1"
            assert scopes == ["s"]
            return _ok(["doc_001"])

    runner = Runner(api=FakeApi())
    results = runner.run(str(golden), top_ks=[1, 3])
    assert len(results) == 1
    assert results[0].qid == "q01"
    assert results[0].expected_doc_id == "doc_001"
    assert results[0].returned_doc_ids == ["doc_001"]
    assert results[0].status_code == 200


def test_runner_records_refusal_via_404(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [
        {"qid": "q01", "question": "Q1", "expected_doc_title": None,
         "scope": ["s"], "expect_refusal": True, "tags": []},
    ])

    class FakeApi:
        def list_documents(self):
            return []

        def chat_query(self, *, question, scopes):
            raise ApiNotFound("not found")

    results = Runner(api=FakeApi()).run(str(golden), top_ks=[1])
    assert results[0].status_code == 404
    assert results[0].expect_refusal is True


def test_runner_handles_http_error(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [
        {"qid": "q01", "question": "Q1", "expected_doc_title": "X",
         "scope": ["s"], "expect_refusal": False, "tags": []},
    ])

    class FakeApi:
        def list_documents(self):
            return [{"doc_id": "d1", "title": "X", "parse_status": "published"}]
        def chat_query(self, *, question, scopes):
            raise ApiError("boom")

    results = Runner(api=FakeApi()).run(str(golden), top_ks=[1])
    assert results[0].error == "boom"
    assert results[0].status_code == 0
