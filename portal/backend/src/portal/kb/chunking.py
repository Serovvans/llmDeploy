"""Разбиение текста страницы на фрагменты (docs/portal-api.md §8.3, §9.13); чистые функции.

Фрагмент — отрезок текста одной страницы и границу страницы не пересекает: функция
получает текст одной страницы и возвращает границы отрезков в символах.
"""

import re

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
