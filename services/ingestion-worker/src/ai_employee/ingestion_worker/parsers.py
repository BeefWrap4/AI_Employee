from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt


@dataclass
class ParsedSection:
    section_path: str
    blocks: list[str] = field(default_factory=list)


class _BaseParser:
    def parse(self, source: str) -> list[ParsedSection]:
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


class NotImplementedParser(_BaseParser):
    """占位解析器：明确返回不支持，不静默失败。"""

    def __init__(self, mime_type: str) -> None:
        self.mime_type = mime_type

    def parse(self, source: str) -> list[ParsedSection]:
        raise NotImplementedError(f"mime_unsupported: {self.mime_type}")


_PARSER_MAP = {
    "text/markdown": MarkdownParser,
    "text/html": HtmlParser,
    "text/plain": TextParser,
}


def get_parser(mime_type: str) -> _BaseParser:
    cls = _PARSER_MAP.get(mime_type)
    if cls is None:
        return NotImplementedParser(mime_type)
    return cls()
