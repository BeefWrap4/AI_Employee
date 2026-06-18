"""Query normalization + reranker tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_employee.knowledge_api.query_normalize import (
    QueryEntities,
    extract_entities,
    normalize_query,
)
from ai_employee.knowledge_api.reranker import (
    CrossEncoderReranker,
    Reranker,
    StubReranker,
    build_reranker,
)
from ai_employee.knowledge_api.retrieval import RetrievalHit


def _hit(content: str, conf: float = 0.5, cid: str = "c1") -> RetrievalHit:
    return RetrievalHit(
        chunk_id=cid, doc_id="d1", doc_title="t", content=content,
        section_path="root", page_no=1, confidence=conf,
    )


# --- query normalize ----------------------------------------------------- #


def test_extract_alarm_code() -> None:
    ents = extract_entities("5G 小区 RRC_SETUP_FAIL_HIGH 升高怎么排查？")
    assert "RRC_SETUP_FAIL_HIGH" in ents.alarm_codes


def test_extract_ne_cell_site_ids() -> None:
    ents = extract_entities("NE-001 的 CELL-002 在 SITE-003 上掉话")
    assert "NE-001" in ents.ne_ids
    assert "CELL-002" in ents.cell_ids
    assert "SITE-003" in ents.site_ids


def test_extract_vendor_and_network_type() -> None:
    ents = extract_entities("华为 5G 基站 RRC 建立失败")
    assert "华为" in ents.vendors
    assert "5g" in ents.network_types
    assert "rrc" in ents.metrics


def test_extract_filters_common_false_positives() -> None:
    ents = extract_entities("请按 SOP 处理，参考 PDF 和 API 文档")
    assert "SOP" not in ents.alarm_codes
    assert "PDF" not in ents.alarm_codes
    assert "API" not in ents.alarm_codes


def test_normalize_query_appends_entities() -> None:
    out = normalize_query("NE-001 RRC_SETUP_FAIL_HIGH 升高")
    assert "NE-001" in out
    assert "RRC_SETUP_FAIL_HIGH" in out
    # Original text preserved.
    assert out.startswith("NE-001 RRC_SETUP_FAIL_HIGH 升高")


def test_normalize_query_no_entities_returns_original() -> None:
    assert normalize_query("今天天气不错") == "今天天气不错"


# --- reranker ------------------------------------------------------------ #


def test_stub_reranker_reorders_by_token_overlap() -> None:
    reranker = StubReranker()
    hits = [
        _hit("完全无关的内容关于天气", conf=0.9, cid="c_unrelated"),
        _hit("RRC 建立失败 先查告警 KPI", conf=0.4, cid="c_related"),
    ]
    out = reranker.rerank("RRC 建立失败 告警 KPI", hits, top_k=2)
    # The related hit should outrank the unrelated one despite lower
    # initial fusion confidence.
    assert out[0].chunk_id == "c_related"


def test_stub_reranker_boosts_entity_mention() -> None:
    reranker = StubReranker()
    hits = [
        _hit("general guidance about alarms", conf=0.5, cid="c_generic"),
        _hit("ALM_2541 处置步骤见下", conf=0.5, cid="c_alarmcode"),
    ]
    out = reranker.rerank("ALM_2541 怎么处理", hits, top_k=2)
    assert out[0].chunk_id == "c_alarmcode"


def test_stub_reranker_respects_top_k() -> None:
    reranker = StubReranker()
    hits = [_hit(f"content {i}", conf=0.5, cid=f"c{i}") for i in range(5)]
    out = reranker.rerank("content", hits, top_k=2)
    assert len(out) == 2


def test_stub_reranker_empty_hits() -> None:
    assert StubReranker().rerank("q", [], top_k=3) == []


def test_stub_reranker_clamps_confidence() -> None:
    reranker = StubReranker()
    hits = [_hit("exact match exact match", conf=1.0, cid="c1")]
    out = reranker.rerank("exact match", hits, top_k=1)
    assert 0.0 <= out[0].confidence <= 1.0


def test_build_reranker_defaults_to_stub(monkeypatch) -> None:
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    assert isinstance(build_reranker(), StubReranker)


def test_build_reranker_returns_crossencoder_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANKER_URL", "http://rerank.local:8088")
    assert isinstance(build_reranker(), CrossEncoderReranker)


def test_crossencoder_falls_back_on_network_error() -> None:
    reranker = CrossEncoderReranker("http://does-not-exist.invalid:8088")
    hits = [_hit("RRC failure handling", conf=0.4, cid="c1")]
    out = reranker.rerank("RRC failure", hits, top_k=1)
    # Falls back to stub behaviour, still returns a hit.
    assert len(out) == 1


def test_crossencoder_uses_remote_scores(monkeypatch) -> None:
    reranker = CrossEncoderReranker("http://rerank.local:8088")
    hits = [
        _hit("a", conf=0.5, cid="c0"),
        _hit("b", conf=0.5, cid="c1"),
    ]
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"scores": [0.2, 0.9]}
    with patch("httpx.post", return_value=fake):
        out = reranker.rerank("q", hits, top_k=2)
    # Higher score first.
    assert out[0].chunk_id == "c1"
    assert out[0].confidence == pytest.approx(0.9)
