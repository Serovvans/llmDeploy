"""Векторный индекс в Qdrant: коллекция, фильтр доступа, гибридный запрос (docs/portal-api.md §10).

Фильтр доступа строится здесь из идентификатора пользователя и выбора клиента и стоит в
каждом запросе пачки — в каждом `prefetch` и в корне; запроса поиска без него нет.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from itertools import batched
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException

from portal.core.settings import KbSettings
from portal.kb.ports import (
    Collection,
    CollectionMismatchError,
    FragmentPoint,
    KnowledgeSelection,
    SearchHits,
    SparseVector,
    VectorIndexUnavailableError,
)
from portal.kb.tags import selected_keys

_DENSE = "dense"
_LEXICAL = "lexical"
_PAYLOAD_INDEXES = (
    ("document_id", models.PayloadSchemaType.KEYWORD),
    ("scope", models.PayloadSchemaType.KEYWORD),
    ("owner_id", models.PayloadSchemaType.KEYWORD),
    ("is_cogis", models.PayloadSchemaType.BOOL),
    ("tags", models.PayloadSchemaType.KEYWORD),
    ("pending", models.PayloadSchemaType.BOOL),
)
# Порядок ветвей фильтра — как в контракте (§10.3).
_COLLECTIONS: tuple[Collection, ...] = ("shared", "ministries", "personal")


def _match(key: str, value: str | bool) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def access_filter(user_id: UUID, selection: KnowledgeSelection) -> models.Filter:
    """Фильтр доступа (§10.3): предел — общая ∪ «Министерства» ∪ личная пользователя.

    Выбор клиента предел только сужает: решает, какие из трёх ветвей войдут в фильтр и
    добавится ли условие по тегам. Ветвь личной коллекции без `owner_id` из сессии не
    существует. Точки запаса (`pending`) в поиск не попадают никогда.
    """
    not_pending = [_match("pending", True)]
    shared = _match("scope", "shared")
    if selection.cogis_only:
        return models.Filter(must=[shared, _match("is_cogis", True)], must_not=not_pending)
    branches: list[models.Condition] = []
    for collection in _COLLECTIONS:
        if collection not in selection.collections:
            continue
        if collection == "personal":
            branches.append(
                models.Filter(must=[_match("scope", "personal"), _match("owner_id", str(user_id))])
            )
        else:
            branches.append(_match("scope", collection))
    if not branches:
        raise ValueError("в выборе нет ни одной известной коллекции")
    must: list[models.Condition] = [models.Filter(should=branches)]
    if keys := selected_keys(selection.tags):
        must.append(models.FieldCondition(key="tags", match=models.MatchAny(any=keys)))
    return models.Filter(must=must, must_not=not_pending)


def _document_filter(
    document_id: UUID, must_not: list[models.Condition] | None = None
) -> models.Filter:
    return models.Filter(must=[_match("document_id", str(document_id))], must_not=must_not)


def _lexical(vector: SparseVector) -> models.SparseVector:
    return models.SparseVector(indices=list(vector.indices), values=list(vector.values))


def _vectors(point: FragmentPoint) -> dict[str, list[float] | models.SparseVector]:
    vectors: dict[str, list[float] | models.SparseVector] = {_DENSE: list(point.dense)}
    if point.lexical.indices:
        vectors[_LEXICAL] = _lexical(point.lexical)
    return vectors


def _staged_point(point: FragmentPoint) -> models.PointStruct:
    return models.PointStruct(
        id=str(point.fragment_id),
        vector=_vectors(point),
        payload={
            "document_id": str(point.document_id),
            "scope": point.scope,
            "owner_id": str(point.owner_id),
            "is_cogis": point.is_cogis,
            "tags": list(point.tags),
            "pending": True,
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

    async def delete_document(self, document_id: UUID) -> None:
        """Удалить все точки документа, включая запас."""
        await self.prune_document(document_id, ())

    async def prune_document(self, document_id: UUID, keep_ids: Sequence[UUID]) -> None:
        """Уборка: удалить точки документа, которых нет в `keep_ids` (§10.6)."""
        kept: list[models.Condition] = [
            models.HasIdCondition(has_id=[str(fragment_id) for fragment_id in keep_ids])
        ]
        selected = _document_filter(document_id, kept if keep_ids else None)
        with _guard():
            await self._client.delete(
                self._collection, points_selector=models.FilterSelector(filter=selected), wait=True
            )

    async def stage_document(self, document_id: UUID, points: Sequence[FragmentPoint]) -> None:
        """Записать точки в запас пачками: с `pending`, поиск их не видит."""
        with _guard():
            for batch in batched(points, self._upsert_batch_size):
                await self._client.upsert(
                    self._collection, [_staged_point(point) for point in batch], wait=True
                )

    async def publish_document(self, document_id: UUID, tags: Sequence[str]) -> None:
        """Выставить теги и снять `pending` у запаса; прежние точки не трогаются."""
        staged = models.Filter(
            must=[_match("document_id", str(document_id)), _match("pending", True)]
        )
        with _guard():
            await self._client.set_payload(
                self._collection, payload={"tags": list(tags)}, points=staged, wait=True
            )
            await self._client.delete_payload(
                self._collection, keys=["pending"], points=staged, wait=True
            )

    async def update_vectors(self, points: Sequence[FragmentPoint]) -> None:
        """Перезаписать векторы точек под теми же идентификаторами; payload не трогается."""
        with _guard():
            for batch in batched(points, self._upsert_batch_size):
                await self._client.update_vectors(
                    self._collection,
                    [
                        models.PointVectors(id=str(point.fragment_id), vector=_vectors(point))
                        for point in batch
                    ],
                    wait=True,
                )

    async def set_tags(self, document_id: UUID, tags: Sequence[str]) -> None:
        """Выставить ключи тегов у всех точек документа, включая запас (§10.6)."""
        with _guard():
            await self._client.set_payload(
                self._collection,
                payload={"tags": list(tags)},
                points=_document_filter(document_id),
                wait=True,
            )

    async def search(
        self,
        dense: Sequence[float],
        lexical: SparseVector,
        user_id: UUID,
        selection: KnowledgeSelection,
        hybrid_limit: int,
    ) -> SearchHits:
        """Поиск двумя запросами одной пачкой (§10.4); фильтр доступа — в каждом из них.

        Лучшие лексические совпадения (`kb.search.lexical_slots`) и до `hybrid_limit`
        результатов слияния ветвей — по отдельности: выдачу собирает сценарий поиска.
        """
        access = access_filter(user_id, selection)
        requests = self._requests(list(dense), lexical, access, hybrid_limit)
        with _guard():
            responses = await self._client.query_batch_points(self._collection, requests)
        found = [[UUID(str(point.id)) for point in response.points] for response in responses]
        # Гибридный запрос — последний в пачке; перед ним лексический, если он был.
        return SearchHits(lexical=found[0] if len(found) > 1 else [], hybrid=found[-1])

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
