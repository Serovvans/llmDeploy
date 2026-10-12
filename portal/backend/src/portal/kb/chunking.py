"""Разбиение текста на фрагменты (docs/portal-api.md §8.3, §9.13, §10.1); чистые функции.

Фрагмент — отрезок текста одной страницы: он не пересекает ни границу страницы, ни
границу раздела. Раздел начинается строкой-заголовком и продолжается на следующих
страницах до нового заголовка; структура определяется по тексту — одинаково для всех
форматов.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

_PARAGRAPH = re.compile(r"\n\s*\n")
_LINE = re.compile(r"\n")
_SENTENCE = re.compile(r"[.!?…;]\s")
_SPACE = re.compile(r"\s")
# Чем раньше в списке, тем лучше место разреза.
_BOUNDARIES = (_PARAGRAPH, _LINE, _SENTENCE, _SPACE)


def _last_boundary(text: str, low: int, high: int) -> int | None:
    """Конец последней границы лучшего вида в `text[low:high]`; `None` — границ нет."""
    for pattern in _BOUNDARIES:
        ends = [match.end() for match in pattern.finditer(text, low, high)]
        if ends:
            return ends[-1]
    return None


def _cut(text: str, start: int, max_chars: int) -> int:
    """Где закончить фрагмент, начатый в `start`: по границе во второй половине окна."""
    hard = start + max_chars
    if hard >= len(text):
        return len(text)
    return _last_boundary(text, start + max_chars // 2, hard) or hard


def _next_start(text: str, start: int, end: int, overlap_chars: int) -> int:
    """Начало следующего фрагмента: с перекрытием, но не с середины слова."""
    position = max(end - overlap_chars, start + 1)
    while position < end and not text[position - 1].isspace():
        position += 1
    return position


def split_page(text: str, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Границы фрагментов страницы: пары (начало, конец) в символах, по порядку.

    Фрагмент не длиннее `max_chars`, без пробелов по краям; соседние перекрываются
    примерно на `overlap_chars`, чтобы фраза на стыке не терялась. Пустая страница
    фрагментов не даёт.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        end = _cut(text, start, max_chars)
        stripped_end = start + len(text[start:end].rstrip())
        if stripped_end > start:
            spans.append((start, stripped_end))
        if end >= len(text):
            break
        start = _next_start(text, start, end, overlap_chars)
    return spans


@dataclass(frozen=True)
class Span:
    """Отрезок страницы — будущий фрагмент — и раздел, к которому он относится."""

    page_number: int
    start: int
    end: int
    section: str | None


def one_line(text: str, max_chars: int) -> str:
    """Название в одну строку и не длиннее `max_chars`: так оно входит в текст для поиска."""
    return " ".join(text.split())[:max_chars]


def _headings(text: str, patterns: Sequence[re.Pattern[str]]) -> list[tuple[int, str]]:
    """Строки-заголовки страницы: начало строки в символах и сама строка.

    Нумерованные строки, идущие подряд без текста между ними, — перечень или оглавление,
    а не заголовки: раздел из одной строки поиску не нужен, перечень остаётся целым.
    """
    found: list[tuple[int, int, str]] = []
    position = 0
    row = 0  # номер строки среди непустых
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            if any(pattern.search(stripped) for pattern in patterns):
                found.append((row, position, stripped))
            row += 1
        position += len(line)
    numbered = {row for row, _, line in found if line[0].isdigit()}
    return [
        (position, line)
        for row, position, line in found
        if not (row in numbered and (row - 1 in numbered or row + 1 in numbered))
    ]


def split_document(
    pages: Sequence[tuple[int, str]],
    heading_patterns: Sequence[str],
    max_chars: int,
    overlap_chars: int,
    section_max_chars: int,
) -> list[Span]:
    """Фрагменты документа по порядку; `pages` — номер и текст каждой страницы.

    Страница делится по строкам-заголовкам (`heading_patterns` применяются к отдельной
    строке без пробелов по краям), каждая часть — прежним разбиением `split_page`.
    Заголовок входит в первый фрагмент своего раздела; текст до первого заголовка
    документа раздела не имеет.
    """
    patterns = [re.compile(pattern) for pattern in heading_patterns]
    spans: list[Span] = []
    section: str | None = None
    for number, text in pages:
        headings = _headings(text, patterns)
        starts = [0, *(start for start, _ in headings)]
        names = [section, *(one_line(line, section_max_chars) for _, line in headings)]
        for begin, end, name in zip(starts, [*starts[1:], len(text)], names, strict=True):
            spans.extend(
                Span(number, begin + start, begin + stop, name)
                for start, stop in split_page(text[begin:end], max_chars, overlap_chars)
            )
        section = names[-1]
    return spans
