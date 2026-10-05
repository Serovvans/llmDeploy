"""Лексическая часть поиска: токены и разреженный вектор (docs/portal-api.md §10.5).

Одна и та же функция строит вектор фрагмента при индексации и вектор запроса при
поиске. Любое изменение правил меняет векторы всех фрагментов: после него обязательна
переиндексация всех документов — `portal reindex`.
"""

import math
import re
import zlib
from functools import lru_cache

import snowballstemmer

from portal.kb.ports import SparseVector

# Шаг 1. Дефисы и тире всех видов — один знак: «14–А» и «14-А» совпадают.
_NORMALIZATION = str.maketrans({"ё": "е", **dict.fromkeys("‐‑‒–—―−", "-")})
# Шаг 2. Знак номера с пробелами вокруг: слева отрезок из букв и цифр, справа — тоже
# (правый только просматривается, чтобы стать левым для следующего знака).
_SPACED_SIGN = re.compile(r"([^\W_]+)[ \t ]*([-/])[ \t ]*(?=([^\W_]+))")
# Шаг 3. Слово или составной номер: отрезки из букв и цифр, соединённые знаками «: / . -»
# (77:01:0004012:345, 14-а, 123/2024-пп, 05.10.2026).
_TOKEN = re.compile(r"[^\W_]+(?:[:/.\-][^\W_]+)*")
_SIGNS = re.compile(r"([:/.\-])")
# Шаг 4. Латинские буквы, которые в заглавном виде неотличимы от кириллических.
_TWINS = str.maketrans("abcehkmoptxy", "авсенкмортху")
_ONLY_TWINS = re.compile(r"[abcehkmoptxy]+")
_CYRILLIC = re.compile(r"[а-я]")
_CYRILLIC_WORD = re.compile(r"[а-я]+")
_DIGIT = re.compile(r"\d")
_STEM_CACHE_SIZE = 100_000

_stemmer = snowballstemmer.stemmer("russian")


@lru_cache(maxsize=_STEM_CACHE_SIZE)
def _stem(word: str) -> str:
    """Основа русского слова по Snowball; слова в текстах повторяются — ответ запоминается."""
    return str(_stemmer.stemWord(word))


def _join_spaced_number(match: re.Match[str]) -> str:
    """«14 - а» → «14-а», если хотя бы один из соседних отрезков содержит цифру."""
    left, sign, right = match.group(1, 2, 3)
    if _DIGIT.search(left) or _DIGIT.search(right):
        return f"{left}{sign}"
    return match.group()


def _untwin(segment: str, in_number: bool) -> str:
    """Заменить буквы-двойники кириллическими, если отрезок — не обычное латинское слово.

    `in_number` — отрезок входит в составной токен с цифрой.
    """
    mixed = _CYRILLIC.search(segment) or _DIGIT.search(segment)
    if mixed or (in_number and _ONLY_TWINS.fullmatch(segment)):
        return segment.translate(_TWINS)
    return segment


def tokenize(text: str) -> list[str]:
    """Токены текста по шагам §10.5.

    Составной номер даёт токен целиком и каждый свой отрезок; чисто кириллический
    отрезок — ещё и основу слова, той же строкой в общем пространстве токенов: основа
    запроса «соколовой» совпадает и с основой «соколова», и с точной формой «соколов».
    """
    prepared = _SPACED_SIGN.sub(_join_spaced_number, text.lower().translate(_NORMALIZATION))
    tokens: list[str] = []
    for match in _TOKEN.finditer(prepared):
        parts = _SIGNS.split(match.group())  # отрезки на чётных местах, знаки — на нечётных
        in_number = len(parts) > 1 and bool(_DIGIT.search(match.group()))
        parts[::2] = [_untwin(segment, in_number) for segment in parts[::2]]
        segments = parts[::2]
        if len(segments) > 1:
            tokens.append("".join(parts))
        tokens.extend(segments)
        for segment in segments:
            if _CYRILLIC_WORD.fullmatch(segment) and (stem := _stem(segment)) != segment:
                tokens.append(stem)
    return tokens


def sparse_vector(text: str) -> SparseVector:
    """Разреженный вектор: индекс — CRC-32 токена, значение — `1 + ln(число вхождений)`.

    CRC-32 — устойчивый 32-битный хеш: не зависит от процесса и версии Python. Обратную
    частоту считает Qdrant (модификатор `idf`), но длину фрагмента он не учитывает:
    без насыщения частоты части номера, повторённые в каждой однотипной выписке по
    несколько раз, перевешивали единственное вхождение целого номера.
    """
    counts: dict[int, int] = {}
    for token in tokenize(text):
        index = zlib.crc32(token.encode())
        counts[index] = counts.get(index, 0) + 1
    values = tuple(1.0 + math.log(count) for count in counts.values())
    return SparseVector(indices=tuple(counts), values=values)
