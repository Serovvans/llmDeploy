"""Порты базы знаний (docs/portal-api.md §13.3).

`KnowledgeBase` — интерфейс для диалогов и инструментов; остальные порты отделяют
конвейер индексации и поиск от клиента Qdrant и HTTP-клиента модели.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

KnowledgeScope = Literal["shared", "shared_and_personal", "cogis"]


class Embedder(Protocol):
    """Эмбеддинги текста."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы фрагментов в порядке текстов. `EmbeddingsUnavailableError`."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Вектор запроса: к тексту добавляется инструкция модели эмбеддингов."""
        ...


@dataclass(frozen=True)
class SparseVector:
    """Разреженный вектор лексической части поиска."""

    indices: Sequence[int]
    values: Sequence[float]


@dataclass(frozen=True)
class FragmentPoint:
    """Точка коллекции: фрагмент с векторами и полями фильтра доступа."""

    fragment_id: UUID
    document_id: UUID
    scope: Literal["shared", "personal"]
    owner_id: UUID
    is_cogis: bool
    dense: Sequence[float]
    lexical: SparseVector


class VectorIndex(Protocol):
    """Векторный индекс. Сбой или отсутствие коллекции — `VectorIndexUnavailableError`."""

    async def ensure_collection(self) -> None:
        """Создать коллекцию и индексы полей, если их нет.

        Коллекция с другими параметрами — `CollectionMismatchError`.
        """
        ...

    async def drop_collection(self) -> None:
        """Удалить коллекцию со всеми точками; отсутствие — не ошибка."""
        ...

    async def replace_document(self, document_id: UUID, points: Sequence[FragmentPoint]) -> None:
        """Заменить все точки документа: прежние удаляются, новые записываются."""
        ...

    async def delete_document(self, document_id: UUID) -> None:
        """Удалить все точки документа."""
        ...

    async def search(
        self,
        dense: Sequence[float],
        lexical: SparseVector,
        user_id: UUID,
        scope: KnowledgeScope,
        limit: int,
    ) -> list[UUID]:
        """Гибридный поиск; фильтр доступа (§10.3) строится внутри, по `user_id` и `scope`."""
        ...


class PageRecognizer(Protocol):
    """Распознавание страниц-сканов моделью; пользуются индексация и разбор документов."""

    async def recognize(self, images: Sequence[bytes]) -> list[str]:
        """Тексты страниц в порядке изображений (PNG, до 8 за вызов).

        На каждое изображение — отдельный запрос к модели (§12.1); пустая строка —
        текста нет. Сбой страницы после повторов — `RecognitionFailedError`.
        """
        ...


@dataclass(frozen=True)
class Source:
    """Источник ответа; поля — как у `Source` в §5.1."""

    n: int
    document_id: UUID
    document_title: str
    scope: Literal["shared", "personal"]
    page: int | None
    fragment_id: UUID
    quote: str


@dataclass(frozen=True)
class Retrieval:
    """Итог поиска для ответа: что нашлось и что передать модели."""

    sources: Sequence[Source]
    rules: str
    context: str


class KnowledgeBase(Protocol):
    """База знаний для диалогов; обязательства — §13.3."""

    async def retrieve(self, query: str, user_id: UUID, scope: KnowledgeScope) -> Retrieval:
        """Найти места для ответа на `query`. Любой сбой — `KnowledgeUnavailableError`."""
        ...

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовая документация CoGIS."""
        ...


class KnowledgeUnavailableError(Exception):
    """Поиск не выполнен: не отвечают эмбеддинги или Qdrant, либо нет коллекции."""


class RecognitionFailedError(Exception):
    """Страница не распознана и после повторов."""


class EmbeddingsUnavailableError(Exception):
    """Служба эмбеддингов не отвечает или вернула не то, что ожидалось."""


class VectorIndexUnavailableError(Exception):
    """Qdrant не отвечает, вернул ошибку, либо коллекции нет."""


class CollectionMismatchError(Exception):
    """Коллекция есть, но её параметры расходятся с конфигурацией (§10.1)."""
