import json
import pytest

from ai_employee.eval.golden import GoldenItem, GoldenLoadError, load_golden


def _write_golden(tmp_path, lines: list[str]) -> str:
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_load_golden_returns_twelve_items(tmp_path) -> None:
    path = _write_golden(
        tmp_path,
        [
            json.dumps({"qid": f"q{i:02d}", "question": f"Q{i}", "expected_doc_title": f"D{i}", "scope": ["s"], "expect_refusal": False, "tags": ["hit"]})
            for i in range(1, 13)
        ],
    )
    items = load_golden(path)
    assert len(items) == 12
    assert items[0].qid == "q01"
    assert items[0].expected_doc_title == "D1"
    assert items[0].expect_refusal is False
    assert items[0].scope == ["s"]


def test_load_golden_real_file() -> None:
    items = load_golden("tests/rag-eval/golden.jsonl")
    assert len(items) == 12
    assert items[0].qid == "q01"
    q07 = next(i for i in items if i.qid == "q07")
    assert q07.expect_refusal is True
    assert q07.expected_doc_title is None


def test_load_golden_missing_file(tmp_path) -> None:
    with pytest.raises(GoldenLoadError):
        load_golden(str(tmp_path / "missing.jsonl"))


def test_load_golden_empty_file(tmp_path) -> None:
    path = _write_golden(tmp_path, [])
    with pytest.raises(GoldenLoadError):
        load_golden(path)


def test_load_golden_duplicate_qid(tmp_path) -> None:
    line = json.dumps({"qid": "q01", "question": "x", "expected_doc_title": "D", "scope": [], "expect_refusal": False, "tags": []})
    with pytest.raises(GoldenLoadError) as exc:
        load_golden(_write_golden(tmp_path, [line, line]))
    assert "重复" in str(exc.value) or "duplicate" in str(exc.value).lower()


def test_load_golden_refusal_must_have_null_title(tmp_path) -> None:
    bad = json.dumps({"qid": "q1", "question": "x", "expected_doc_title": "D", "scope": [], "expect_refusal": True, "tags": []})
    with pytest.raises(GoldenLoadError) as exc:
        load_golden(_write_golden(tmp_path, [bad]))
    assert "refusal" in str(exc.value).lower() or "拒答" in str(exc.value)


def test_load_golden_hit_must_have_title(tmp_path) -> None:
    bad = json.dumps({"qid": "q1", "question": "x", "expected_doc_title": None, "scope": [], "expect_refusal": False, "tags": []})
    with pytest.raises(GoldenLoadError) as exc:
        load_golden(_write_golden(tmp_path, [bad]))
    assert "title" in str(exc.value).lower() or "标题" in str(exc.value)


def test_load_golden_blank_question(tmp_path) -> None:
    bad = json.dumps({"qid": "q1", "question": "  ", "expected_doc_title": "D", "scope": [], "expect_refusal": False, "tags": []})
    with pytest.raises(GoldenLoadError):
        load_golden(_write_golden(tmp_path, [bad]))


def test_golden_item_fields() -> None:
    item = GoldenItem(qid="q01", question="x", expected_doc_title="D", scope=["s"], expect_refusal=False, tags=["hit"])
    assert item.qid == "q01" and item.expected_doc_title == "D"
