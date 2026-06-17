import pytest

from ai_employee.ingestion_worker.parsers import (
    DocxParser,
    HtmlParser,
    MarkdownParser,
    NotImplementedParser,
    ParsedSection,
    PdfParser,
    TextParser,
    XlsxParser,
    get_parser,
)


def test_text_parser_splits_by_blank_lines() -> None:
    sections = TextParser().parse("第一段。\n\n第二段。")
    assert len(sections) == 1
    assert sections[0].section_path == "root"
    assert sections[0].blocks == ["第一段。", "第二段。"]


def test_text_parser_empty_input_returns_empty() -> None:
    assert TextParser().parse("") == []
    assert TextParser().parse("   \n\n  ") == []


def test_markdown_parser_builds_section_path() -> None:
    md = (
        "# 接入排障\n"
        "## RRC 建立失败\n"
        "先检查告警和 KPI。\n\n"
        "再检查传输链路。\n"
    )
    sections = MarkdownParser().parse(md)
    paths = {s.section_path for s in sections}
    assert "接入排障 > RRC 建立失败" in paths
    all_blocks = [b for s in sections for b in s.blocks]
    assert any("告警" in b for b in all_blocks)
    assert any("传输链路" in b for b in all_blocks)


def test_markdown_parser_blocks_below_root_when_no_heading() -> None:
    sections = MarkdownParser().parse("裸文本段落。")
    assert sections[0].section_path == "root"


def test_html_parser_uses_headings_for_section_path() -> None:
    html = (
        "<html><body>"
        "<h1>接入排障</h1>"
        "<p>先检查告警。</p>"
        "<h2>RRC</h2>"
        "<p>再检查 KPI。</p>"
        "</body></html>"
    )
    sections = HtmlParser().parse(html)
    paths = {s.section_path for s in sections}
    assert "接入排障" in paths
    assert "接入排障 > RRC" in paths
    all_blocks = [b for s in sections for b in s.blocks]
    assert any("告警" in b for b in all_blocks)


def test_html_parser_strips_tags() -> None:
    sections = HtmlParser().parse("<p><b>带标签</b>正文</p>")
    assert sections[0].blocks[0] == "带标签正文"


def test_not_implemented_parser_raises() -> None:
    with pytest.raises(NotImplementedError) as exc:
        NotImplementedParser(mime_type="application/pdf").parse("")
    assert "application/pdf" in str(exc.value)


def test_get_parser_routes_by_mime() -> None:
    assert isinstance(get_parser("text/markdown"), MarkdownParser)
    assert isinstance(get_parser("text/html"), HtmlParser)
    assert isinstance(get_parser("text/plain"), TextParser)
    assert isinstance(get_parser("application/pdf"), PdfParser)
    assert isinstance(get_parser(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ), DocxParser)
    assert isinstance(get_parser(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ), XlsxParser)
    assert isinstance(get_parser("application/octet-stream"), NotImplementedParser)


def test_parsed_section_holds_fields() -> None:
    sec = ParsedSection(section_path="root", blocks=["x"])
    assert sec.section_path == "root"
    assert sec.blocks == ["x"]
