"""Векторный индекс в Qdrant: коллекция, фильтр доступа, гибридный запрос (docs/portal-api.md §10).

Фильтр доступа строится здесь из идентификатора пользователя и области поиска и стоит в
каждом запросе пачки — в каждом `prefetch` и в корне; запроса к коллекции без него нет.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from itertools import batched
from typing import assert_never
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException

from portal.core.settings import KbSettings
from portal.kb.ports import (
    CollectionMismatchError,
    FragmentPoint,
    KnowledgeScope,
    SparseVector,
    VectorIndexUnavailableError,
)

_DENSE = "dense"
_LEXICAL = "lexical"
_PAYLOAD_INDEXES = (
    ("document_id", models.PayloadSchemaType.KEYWORD),
    ("scope", models.PayloadSchemaType.KEYWORD),
    ("owner_id", models.PayloadSchemaType.KEYWORD),
    ("is_cogis", models.PayloadSchemaType.BOOL),
)


def _match(key: str, value: str | bool) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def access_filter(user_id: UUID, scope: KnowledgeScope) -> models.Filter:
    """Фильтр доступа (§10.3): один из трёх вариантов, других нет.

    Значения берутся только из сессии и вида диалога; неизвестная область — ошибка, а не
    запрос без фильтра.
    """
    shared = _match("scope", "shared")
    if scope == "shared":
        return models.Filter(must=[shared])
    if scope == "cogis":
        return models.Filter(must=[shared, _match("is_cogis", True)])
    if scope == "shared_and_personal":
        own = models.Filter(must=[_match("scope", "personal"), _match("owner_id", str(user_id))])
        return models.Filter(must=[models.Filter(should=[shared, own])])
    assert_never(scope)


def _document_filter(document_id: UUID) -> models.Filter:
    return models.Filter(must=[_match("document_id", str(document_id))])


def _lexical(vector: SparseVector) -> models.SparseVector:
    return models.SparseVector(indices=list(vector.indices), values=list(vector.values))


def _point(point: FragmentPoint) -> models.PointStruct:
    vectors: dict[str, list[float] | models.SparseVector] = {_DENSE: list(point.dense)}
    if point.lexical.indices:
        vectors[_LEXICAL] = _lexical(point.lexical)
    return models.PointStruct(
        id=str(point.fragment_id),
        vector=vectors,
        payload={
            "document_id": str(point.document_id),
            "scope": point.scope,
            "owner_id": str(point.owner_id),
            "is_cogis": point.is_cogis,
        },
    )


@contextmanager
def _guard() -> Iterator[None]:
    """Сбой Qdrant → исключение порта.

    `ValueError` — так локальный режим клиента сообщает об отсутствии коллекции.
    """
    try:
        yield
    except (ApiException, ValueError) as error:
        raise VectorIndexUnavailableError from error


class QdrantVectorIndex:
    """Реализация порта `VectorIndex` на клиенте Qdrant."""

    def __init__(
        self, client: AsyncQdrantClient, settings: KbSettings, collection: str | None = None
    ) -> None:
        """Получить клиент и параметры коллекции и поиска.

        `collection` — имя вместо `kb.qdrant.collection`: отдельная коллекция оценки поиска.
        """
        self._client = client
        self._collection = collection or settings.qdrant.collection
        self._dimension = settings.embeddings.dimension
        self._prefetch_limit = settings.search.prefetch_limit
        self._lexical_slots = settings.search.lexical_slots
        self._upsert_batch_size = settings.qdrant.upsert_batch_size

    async def ensure_collection(self) -> None:
        """Создать коллекцию и индексы полей, если их нет; чужие параметры — отказ."""
        with _guard():
            if await self._client.collection_exists(self._collection):
                self._check((await self._client.get_collection(self._collection)).config.params)
            else:
                await self._client.create_collection(
                    self._collection,
                    vectors_config={
                        _DENSE: models.VectorParams(
                            size=self._dimension, distance=models.Distance.COSINE
                        )
                    },
                    sparse_vectors_config={
                        _LEXICAL: models.SparseVectorParams(modifier=models.Modifier.IDF)
                    },
                )
            for field, schema in _PAYLOAD_INDEXES:
                await self._client.create_payload_index(self._collection, field, schema, wait=True)

    def _check(self, params: models.CollectionParams) -> None:
        dense = params.vectors.get(_DENSE) if isinstance(params.vectors, dict) else None
        lexical = (params.sparse_vectors or {}).get(_LEXICAL)
        matches = (
            dense is not None
            and dense.size == self._dimension
            and dense.distance == models.Distance.COSINE
            and lexical is not None
            and lexical.modifier == models.Modifier.IDF
        )
        if not matches:
            raise CollectionMismatchError

    async def drop_collection(self) -> None:
        """Удалить коллекцию со всеми точками; отсутствие — не ошибка."""
        with _guard():
            await self._client.delete_collection(self._collection)

    async def replace_document(self, document_id: UUID, points: Sequence[FragmentPoint]) -> None:
        """Удалить прежние точки документа и записать новые пачками."""
        await self.delete_document(document_id)
        with _guard():
            for batch in batched(points, self._upsert_batch_size):
                await self._client.upsert(
                    self._collection, [_point(point) for point in batch], wait=True
                )

    async def delete_document(self, document_id: UUID) -> None:
        """Удалить все точки документа."""
        with _guard():
            await self._client.delete(
                self._collection,
                points_selector=models.FilterSelector(filter=_document_filter(document_id)),
                wait=True,
            )

    async def search(
        self,
        dense: Sequence[float],
        lexical: SparseVector,
        user_id: UUID,
        scope: KnowledgeScope,
        limit: int,
    ) -> list[UUID]:
        """Поиск двумя запросами одной пачкой (§10.4); фильтр доступа — в каждом из них.

        Сначала идут лучшие лексические совпадения (`kb.search.lexical_slots`), затем
        результаты гибридного запроса без повторов — всего не больше `limit`.
        """
        access = access_filter(user_id, scope)
        requests = self._requests(list(dense), lexical, access, limit)
        with _guard():
            responses = await self._client.query_batch_points(self._collection, requests)
        # Запросы идут от лексического к гибридному: в этом же порядке собирается выдача.
        found = dict.fromkeys(
            UUID(str(point.id)) for response in responses for point in response.points
        )
        return list(found)[:limit]

    def _requests(
        self, dense: list[float], lexical: SparseVector, access: models.Filter, limit: int
    ) -> list[models.QueryRequest]:
        """Запросы пачки; у вопроса без единого слова — один векторный."""
        if not lexical.indices:
            return [
                models.QueryRequest(
                    query=dense, using=_DENSE, filter=access, limit=limit, with_payload=False
                )
            ]
        sparse = _lexical(lexical)
        hybrid = models.QueryRequest(
            prefetch=[
                models.Prefetch(
                    query=dense, using=_DENSE, limit=self._prefetch_limit, filter=access
                ),
                models.Prefetch(
                    query=sparse, using=_LEXICAL, limit=self._prefetch_limit, filter=access
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            filter=access,
            limit=limit,
            with_payload=False,
        )
        if not self._lexical_slots:
            return [hybrid]
        exact = models.QueryRequest(
            query=sparse,
            using=_LEXICAL,
            filter=access,
            limit=self._lexical_slots,
            with_payload=False,
        )
        return [exact, hybrid]
