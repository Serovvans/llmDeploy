"""Правила тегов документов (docs/portal-api.md §8.5); чистые функции.

Сравнение, отбор и поиск идут только по ключу — тегу в нижнем регистре; написание
хранится как введено и нужно для показа.
"""

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

TagsErrorCode = Literal["too_many", "too_long", "invalid_format"]

# Управляющие и невидимые символы, суррогаты, разделители строк и абзацев: в теге им
# не место — он показывается в одну строку, а суррогат не сохранить в базе.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class TagsError(Exception):
    """Список тегов нарушает правила; `code` — код ошибки поля `tags`."""

    def __init__(self, code: TagsErrorCode) -> None:
        """Запомнить код."""
        super().__init__(code)
        self.code: TagsErrorCode = code


@dataclass(frozen=True)
class Tag:
    """Тег документа: ключ для сравнения и написание для показа."""

    key: str
    text: str


def tag_key(tag: str) -> str:
    """Ключ тега: без пробелов по краям, в нижнем регистре."""
    return tag.strip().lower()


def normalize_tags(raw: Sequence[str], *, max_count: int, max_length: int) -> list[Tag]:
    """Проверить теги и слить повторы по ключу; остаётся первое написание.

    Нарушение — `TagsError`: пустой тег или недопустимые символы — `invalid_format`,
    тег длиннее `max_length` — `too_long`, тегов больше `max_count` — `too_many`.
    """
    tags: dict[str, Tag] = {}
    for item in raw:
        text = item.strip()
        if not text or any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in text):
            raise TagsError("invalid_format")
        if len(text) > max_length:
            raise TagsError("too_long")
        tags.setdefault(text.lower(), Tag(text.lower(), text))
    if len(tags) > max_count:
        raise TagsError("too_many")
    return list(tags.values())


def selected_keys(tags: Iterable[str]) -> list[str]:
    """Ключи тегов выбора (§10.3) без повторов, по алфавиту; пустые строки отброшены."""
    return sorted({tag_key(tag) for tag in tags} - {""})
