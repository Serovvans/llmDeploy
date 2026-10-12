"""Порты базы знаний (docs/portal-api.md §13.3).

`KnowledgeBase` — интерфейс для диалогов и инструментов; остальные порты отделяют
конвейер индексации и поиск от клиента Qdrant и HTTP-клиента модели.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, get_args
from uuid import UUID

Collection = Literal["shared", "personal", "ministries"]
COLLECTIONS: frozenset[Collection] = frozenset(get_args(Collection))


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
    scope: Collection
    owner_id: UUID
    is_cogis: bool
    tags: Sequence[str]  # ключи тегов документа (§8.5)
    dense: Sequence[float]
    lexical: SparseVector


@dataclass(frozen=True)
class KnowledgeSelection:
    """Выбор клиента: коллекции и теги. Предел доступа он может только сузить (§10.3).

    `tags` — теги как прислал клиент; ключи строит порт. `cogis_only` — только
    документация CoGIS общей базы: выбор клиента в таком поиске не участвует.
    """

    collections: frozenset[Collection]
    tags: frozenset[str] = frozenset()
    cogis_only: bool = False

    def __post_init__(self) -> None:
        """Пустой набор и неизвестная коллекция — ошибка, а не поиск без ветви фильтра."""
        if not self.collections or not self.collections <= COLLECTIONS:
            raise ValueError("набор коллекций пуст или содержит неизвестное значение")
        if self.cogis_only and (self.collections != {"shared"} or self.tags):
            raise ValueError("документация CoGIS ищется только в общей базе и без тегов")


@dataclass(frozen=True)
class SearchHits:
    """Кандидаты поиска (§10.4)."""

    lexical: Sequence[UUID]  # лучшие лексические совпадения, по порядку
    hybrid: Sequence[UUID]  # слияние ветвей, по порядку

    def ordered(self) -> list[UUID]:
        """Все кандидаты без повторов: сначала лексические совпадения, затем слияние ветвей."""
        return list(dict.fromkeys([*self.lexical, *self.hybrid]))


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

    async def delete_document(self, document_id: UUID) -> None:
        """Удалить все точки документа, включая запас."""
        ...

    async def prune_document(self, document_id: UUID, keep_ids: Sequence[UUID]) -> None:
        """Уборка (§10.6): удалить точки документа, которых нет в `keep_ids`.

        `keep_ids` — текущие `kb_fragments`; пустой список — удаляются все точки документа.
        """
        ...

    async def stage_document(self, document_id: UUID, points: Sequence[FragmentPoint]) -> None:
        """Записать точки в запас: с `pending`, поиск их не видит."""
        ...

    async def publish_document(self, document_id: UUID, tags: Sequence[str]) -> None:
        """Выставить теги и снять `pending` у запаса; прежние точки не трогаются."""
        ...

    async def update_vectors(self, points: Sequence[FragmentPoint]) -> None:
        """Перезаписать векторы существующих точек; payload не меняется (этап контекста, §10.6)."""
        ...

    async def set_tags(self, document_id: UUID, tags: Sequence[str]) -> None:
        """Выставить ключи тегов у всех точек документа (правка тегов, §8.5)."""
        ...

    async def search(
        self,
        dense: Sequence[float],
        lexical: SparseVector,
        user_id: UUID,
        selection: KnowledgeSelection,
        hybrid_limit: int,
    ) -> SearchHits:
        """Гибридный поиск; фильтр §10.3 строится внутри, по `user_id` и `selection`."""
        ...


class Reranker(Protocol):
    """Модель переранжирования; протокол — §12.1."""

    async def scores(self, query: str, documents: Sequence[str]) -> list[float]:
        """Оценки в порядке документов. Сбой — `RerankUnavailableError`."""
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
    scope: Collection
    page: int | None
    timecode_seconds: int | None  # фрагмент расшифровки записи
    image_id: UUID | None  # источник-картинка
    fragment_id: UUID
    quote: str


@dataclass(frozen=True)
class Retrieval:
    """Итог поиска для ответа: что нашлось и что передать модели."""

    sources: Sequence[Source]
    rules: str
    context: str


@dataclass(frozen=True)
class DialogTurn:
    """Сообщение истории диалога: по истории порт переписывает запрос для поиска."""

    role: Literal["user", "assistant"]
    text: str


class KnowledgeBase(Protocol):
    """База знаний для диалогов; обязательства — §13.3."""

    async def retrieve(
        self,
        query: str,
        user_id: UUID,
        selection: KnowledgeSelection,
        history: Sequence[DialogTurn] = (),
    ) -> Retrieval:
        """Найти места для ответа на `query`. Любой сбой — `KnowledgeUnavailableError`."""
        ...

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовая документация CoGIS."""
        ...


class KnowledgeUnavailableError(Exception):
    """Поиск не выполнен: не отвечают эмбеддинги или Qdrant, либо нет коллекции."""


class RecognitionFailedError(Exception):
    """Страница не распознана и после повторов.

    `cause` — что случилось с моделью: `overloaded` — перегружена, `unavailable` —
    недоступна или оборвала ответ (§13.3). Индексация причину не различает.
    """

    def __init__(self, cause: Literal["overloaded", "unavailable"] = "unavailable") -> None:
        """Запомнить причину."""
        super().__init__(cause)
        self.cause = cause


class RerankUnavailableError(Exception):
    """Реранкер недоступен: поиск продолжается в прежнем порядке (§8.4)."""


class EmbeddingsUnavailableError(Exception):
    """Служба эмбеддингов не отвечает или вернула не то, что ожидалось."""


class VectorIndexUnavailableError(Exception):
    """Qdrant не отвечает, вернул ошибку, либо коллекции нет."""


class CollectionMismatchError(Exception):
    """Коллекция есть, но её параметры расходятся с конфигурацией (§10.1)."""
