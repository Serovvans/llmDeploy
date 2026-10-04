"""Постраничная выдача списков (docs/portal-api.md §1.4, вид «по страницам»)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

SortOrder = Literal["asc", "desc"]


@dataclass(frozen=True)
class PageQuery:
    """Запрос страницы списка."""

    page: int
    page_size: int
    q: str | None
    sort: str | None
    order: SortOrder | None

    @property
    def offset(self) -> int:
        """Сколько строк пропустить."""
        return (self.page - 1) * self.page_size


@dataclass(frozen=True)
class Page[T]:
    """Страница списка с общим числом строк."""

    items: Sequence[T]
    page: int
    page_size: int
    total: int
