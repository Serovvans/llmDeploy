"""Разбор Markdown на блоки: заголовки, абзацы, списки, таблицы, блоки кода.

Нужен там, где ответ модели читает сервер: экспорт в DOCX (§5.9) и поиск блоков `sql`
для проверки запроса (§7.1). Разбор намеренно простой и терпимый: всё, что не узнано,
остаётся абзацем текста; HTML не интерпретируется.
"""

import re
from dataclasses import dataclass

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)")
# Выражения без соседних повторов по одному классу символов: разбор строки линеен.
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_LIST_ITEM = re.compile(r"^[ \t]*(?:([-*+])|(\d{1,9})[.)])[ \t]+(.*)$")
_TABLE_RULE_CELL = re.compile(r":?-+:?")
_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|`[^`\n]+`|\*[^*\s][^*\n]*\*)")


@dataclass(frozen=True)
class Heading:
    """Заголовок уровня 1–6."""

    level: int
    text: str


@dataclass(frozen=True)
class Paragraph:
    """Абзац текста."""

    text: str


@dataclass(frozen=True)
class ListBlock:
    """Список; вложенность не различается."""

    ordered: bool
    items: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    """Таблица: первая строка — заголовок."""

    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Code:
    """Ограждённый блок кода; `closed = False` — блок оборван концом текста.

    `line` — номер строки текста, на которой блок открывается, с единицы.
    """

    language: str
    text: str
    closed: bool
    line: int


type Block = Heading | Paragraph | ListBlock | Table | Code


@dataclass(frozen=True)
class Span:
    """Отрезок строки с оформлением."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


def _cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _heading_text(raw: str) -> str:
    """Текст заголовка без закрывающих решёток (`## Заголовок ##`)."""
    return raw.rstrip(" \t").rstrip("#").rstrip(" \t")


def _is_table_rule(line: str) -> bool:
    """Строка-разделитель таблицы: ячейки из дефисов с необязательными двоеточиями."""
    stripped = line.strip(" \t")
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return all(_TABLE_RULE_CELL.fullmatch(cell.strip(" \t")) for cell in stripped.split("|"))


def parse_blocks(text: str) -> list[Block]:
    """Блоки текста по порядку."""
    lines = text.split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Paragraph("\n".join(paragraph)))
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if fence is not None:
            flush()
            marker = fence[1]
            opened_at = index + 1
            body: list[str] = []
            closed = False
            index += 1
            while index < len(lines):
                closing = _FENCE.match(lines[index])
                stripped = lines[index].strip()
                if (
                    closing is not None
                    and closing[1][0] == marker[0]
                    and len(stripped) >= len(marker)
                    and not stripped.strip(marker[0])
                ):
                    closed = True
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            blocks.append(Code(fence[2].lower(), "\n".join(body), closed, opened_at))
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            flush()
            blocks.append(Heading(len(heading[1]), _heading_text(heading[2])))
        elif "|" in line and index + 1 < len(lines) and _is_table_rule(lines[index + 1]):
            flush()
            rows = [_cells(line)]
            index += 2
            # Строка с ограждением открывает блок кода, даже если в ней есть `|` (§7.1).
            while index < len(lines) and "|" in lines[index] and not _FENCE.match(lines[index]):
                rows.append(_cells(lines[index]))
                index += 1
            blocks.append(Table(tuple(rows)))
            continue
        elif (item := _LIST_ITEM.match(line)) is not None:
            flush()
            ordered = item[2] is not None
            items = [item[3]]
            index += 1
            # Строка-ограждение прерывает пункт списка и открывает блок кода (§7.1, шаг 5).
            while index < len(lines) and lines[index].strip() and not _FENCE.match(lines[index]):
                following = _LIST_ITEM.match(lines[index])
                if following is not None:
                    items.append(following[3])
                else:
                    items[-1] += " " + lines[index].strip()
                index += 1
            blocks.append(ListBlock(ordered, tuple(items)))
            continue
        elif not line.strip():
            flush()
        else:
            paragraph.append(line.strip())
        index += 1
    flush()
    return blocks


def parse_spans(text: str) -> list[Span]:
    """Строка отрезками: полужирный, курсив и встроенный код; остальное — как есть."""
    spans: list[Span] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if len(part) > 4 and (part.startswith("**") or part.startswith("__")):
            spans.append(Span(part[2:-2], bold=True))
        elif len(part) > 2 and part.startswith("`") and part.endswith("`"):
            spans.append(Span(part[1:-1], code=True))
        elif len(part) > 2 and part.startswith("*") and part.endswith("*"):
            spans.append(Span(part[1:-1], italic=True))
        else:
            spans.append(Span(part))
    return spans
