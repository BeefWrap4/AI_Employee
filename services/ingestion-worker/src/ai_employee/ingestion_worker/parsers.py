from __future__ import annotations

import io as _io
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt


@dataclass
class ParsedSection:
    section_path: str
    blocks: list[str] = field(default_factory=list)
    # Table structure (xlsx / csv-style structured docs).  Empty for plain
    # prose sources.  ``table_id`` identifies the source table (typically
    # the sheet name); ``row_ids`` are 1-based row identifiers parallel
    # to ``blocks`` (``row_ids[i]`` is the row that produced
    # ``blocks[i]``).
    table_id: str | None = None
    row_ids: list[str] = field(default_factory=list)


class _BaseParser:
    def parse(self, source: str | bytes) -> list[ParsedSection]:
        raise NotImplementedError


class TextParser(_BaseParser):
    """纯文本：按空行分段，section_path 固定 root。"""

    def parse(self, source: str) -> list[ParsedSection]:
        blocks = [b.strip() for b in source.split("\n\n") if b.strip()]
        if not blocks:
            return []
        return [ParsedSection(section_path="root", blocks=blocks)]


class MarkdownParser(_BaseParser):
    """Markdown：按标题层级构造 section_path，正文段落作为 block。"""

    def parse(self, source: str) -> list[ParsedSection]:
        md = MarkdownIt()
        tokens = md.parse(source)
        sections: list[ParsedSection] = []
        heading_stack: list[str] = []
        inline_buffer: list[str] = []
        in_heading = False

        def current_path() -> str:
            return " > ".join(heading_stack) if heading_stack else "root"

        def flush_buffer() -> None:
            if not inline_buffer:
                return
            text = " ".join(inline_buffer).strip()
            inline_buffer.clear()
            if not text:
                return
            if sections and sections[-1].section_path == current_path():
                sections[-1].blocks.append(text)
            else:
                sections.append(ParsedSection(section_path=current_path(), blocks=[text]))

        for token in tokens:
            ttype = token.type
            if ttype == "heading_open":
                flush_buffer()
                in_heading = True
            elif ttype == "heading_close":
                heading_text = " ".join(inline_buffer).strip()
                inline_buffer.clear()
                if heading_text:
                    level = int(token.tag[1])
                    heading_stack = heading_stack[: level - 1]
                    heading_stack.append(heading_text)
                in_heading = False
            elif ttype == "inline":
                if token.content.strip():
                    inline_buffer.append(token.content.strip())
            elif ttype in {"paragraph_close", "bullet_list_close", "ordered_list_close"}:
                if not in_heading:
                    flush_buffer()

        flush_buffer()
        return sections


class HtmlParser(_BaseParser):
    """HTML：按 h1/h2/h3 切片，去除标签。"""

    _HEADING_TAGS = {"h1", "h2", "h3"}

    def parse(self, source: str) -> list[ParsedSection]:
        soup = BeautifulSoup(source, "html.parser")
        sections: list[ParsedSection] = []
        heading_stack: list[str] = []

        def current_path() -> str:
            return " > ".join(heading_stack) if heading_stack else "root"

        def append_block(text: str) -> None:
            text = text.strip()
            if not text:
                return
            if sections and sections[-1].section_path == current_path():
                sections[-1].blocks.append(text)
            else:
                sections.append(ParsedSection(section_path=current_path(), blocks=[text]))

        for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
            if el.name in self._HEADING_TAGS:
                heading_text = el.get_text(strip=True)
                if not heading_text:
                    continue
                level = int(el.name[1])
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(heading_text)
            else:
                text = el.get_text(separator="", strip=True)
                if text:
                    append_block(text)

        return sections


class PdfParser(_BaseParser):
    """PDF：使用 pymupdf 提取文本，按页分组，section_path 为 "Page {n}"。"""

    def parse(self, source: str | bytes) -> list[ParsedSection]:
        import fitz

        if isinstance(source, str):
            data = source.encode("utf-8")
        else:
            data = source

        sections: list[ParsedSection] = []
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            for page_num in range(1, len(doc) + 1):
                page = doc[page_num - 1]
                text = page.get_text("text")
                blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
                if not blocks and text.strip():
                    blocks = [text.strip()]
                if blocks:
                    sections.append(
                        ParsedSection(
                            section_path=f"Page {page_num}",
                            blocks=blocks,
                        )
                    )
        finally:
            doc.close()
        return sections


class DocxParser(_BaseParser):
    """DOCX：使用 python-docx 按段落遍历，heading 样式构建 section_path，正文段落作为 blocks。"""

    def parse(self, source: str | bytes) -> list[ParsedSection]:
        import docx as _docx

        if isinstance(source, str):
            data = source.encode("utf-8")
        else:
            data = source

        doc = _docx.Document(_io.BytesIO(data))
        sections: list[ParsedSection] = []
        heading_stack: list[str] = []

        def current_path() -> str:
            return " > ".join(heading_stack) if heading_stack else "root"

        def append_block(text: str) -> None:
            text = text.strip()
            if not text:
                return
            if sections and sections[-1].section_path == current_path():
                sections[-1].blocks.append(text)
            else:
                sections.append(ParsedSection(section_path=current_path(), blocks=[text]))

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""
            # Detect heading styles: "Heading 1", "Heading 2", "Heading 3", etc.
            if style_name.startswith("Heading ") and style_name[8:].isdigit():
                try:
                    level = int(style_name[8:])
                except ValueError:
                    level = 1
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(text)
            else:
                append_block(text)

        return sections


class XlsxParser(_BaseParser):
    """XLSX：使用 openpyxl 读取每个 sheet，首行为表头，其余行转 "col: value" 文本块。"""

    def parse(self, source: str | bytes) -> list[ParsedSection]:
        import openpyxl

        if isinstance(source, str):
            data = source.encode("utf-8")
        else:
            data = source

        wb = openpyxl.load_workbook(_io.BytesIO(data), read_only=True)
        sections: list[ParsedSection] = []

        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    continue

                headers = [str(h) if h is not None else "" for h in rows[0]]
                data_rows = rows[1:]
                if not data_rows:
                    # sheet has only headers, no data blocks
                    continue

                blocks: list[str] = []
                row_ids: list[str] = []
                for row_idx, row in enumerate(data_rows, start=1):
                    parts: list[str] = []
                    for j, val in enumerate(row):
                        col_name = headers[j] if j < len(headers) else f"Col{j}"
                        val_str = str(val) if val is not None else ""
                        parts.append(f"{col_name}: {val_str}")
                    block = " | ".join(parts)
                    if block.strip():
                        blocks.append(block)
                        row_ids.append(f"row_{row_idx:03d}")

                if blocks:
                    sections.append(
                        ParsedSection(
                            section_path=sheet_name,
                            blocks=blocks,
                            table_id=sheet_name,
                            row_ids=row_ids,
                        )
                    )
        finally:
            wb.close()

        return sections


class NotImplementedParser(_BaseParser):
    """占位解析器：明确返回不支持，不静默失败。"""

    def __init__(self, mime_type: str) -> None:
        self.mime_type = mime_type

    def parse(self, source: str | bytes) -> list[ParsedSection]:
        raise NotImplementedError(f"mime_unsupported: {self.mime_type}")


_PARSER_MAP = {
    "text/markdown": MarkdownParser,
    "text/html": HtmlParser,
    "text/plain": TextParser,
    "application/pdf": PdfParser,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocxParser,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": XlsxParser,
}


def get_parser(mime_type: str) -> _BaseParser:
    cls = _PARSER_MAP.get(mime_type)
    if cls is None:
        return NotImplementedParser(mime_type)
    return cls()
