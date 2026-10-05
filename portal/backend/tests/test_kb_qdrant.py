"""Векторный индекс на движке Qdrant: коллекция, фильтр доступа, гибридный запрос (§10).

По умолчанию — локальный режим клиента Qdrant: тот же код запросов и фильтров, что у
сервера, но в памяти процесса, без сети и Docker. С переменной `PORTAL_TEST_QDRANT_URL`
(и `PORTAL_TEST_QDRANT_API_KEY`) те же тесты идут ещё и на настоящем сервере Qdrant.
"""

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from llm_stub import replies
from qdrant_client import AsyncQdrantClient, models

from portal.core.settings import Settings
from portal.kb.lexical import sparse_vector
from portal.kb.ports import (
    CollectionMismatchError,
    FragmentPoint,
    KnowledgeScope,
    SparseVector,
    VectorIndexUnavailableError,
)
from portal.kb.qdrant import QdrantVectorIndex, access_filter

pytestmark = pytest.mark.anyio

SERVER_URL = os.environ.get("PORTAL_TEST_QDRANT_URL")
ENGINES = ["local", *(["server"] if SERVER_URL else [])]
ANN, BORIS = uuid4(), uuid4()


@pytest.fixture(params=ENGINES)
async def client(request: pytest.FixtureRequest) -> AsyncIterator[AsyncQdrantClient]:
    if request.param == "local":
        qdrant = AsyncQdrantClient(location=":memory:")
    else:
        qdrant = AsyncQdrantClient(
            url=SERVER_URL,
            api_key=os.environ.get("PORTAL_TEST_QDRANT_API_KEY"),
            check_compatibility=False,
        )
    yield qdrant
    await qdrant.close()


@pytest.fixture
async def index(client: AsyncQdrantClient, settings: Settings) -> AsyncIterator[QdrantVectorIndex]:
    built = QdrantVectorIndex(client, settings.kb, f"kb_test_{uuid4().hex}")
    await built.ensure_collection()
    yield built
    await built.drop_collection()


def _point(
    text: str, scope: str, owner: UUID, *, is_cogis: bool = False, document: UUID | None = None
) -> FragmentPoint:
    return FragmentPoint(
        fragment_id=uuid4(),
        document_id=document or uuid4(),
        scope=scope,  # type: ignore[arg-type]
        owner_id=owner,
        is_cogis=is_cogis,
        dense=replies.embedding(text) or [],
        lexical=sparse_vector(text),
    )


async def _search(
    index: QdrantVectorIndex, query: str, user: UUID, scope: KnowledgeScope, limit: int = 8
) -> list[UUID]:
    return await index.search(
        replies.embedding(query) or [], sparse_vector(query), user, scope, limit
    )


async def _store(index: QdrantVectorIndex, *points: FragmentPoint) -> None:
    for point in points:
        await index.replace_document(point.document_id, [point])


async def test_foreign_personal_fragment_is_never_returned(index: QdrantVectorIndex) -> None:
    text = "секретный договор аренды участка 77:01:0004012:345"
    shared = _point(text, "shared", BORIS)
    ann_own = _point(text, "personal", ANN)
    boris_own = _point(text, "personal", BORIS)
    await _store(index, shared, ann_own, boris_own)

    found = await _search(index, text, ANN, "shared_and_personal")
    assert set(found) == {shared.fragment_id, ann_own.fragment_id}
    assert set(await _search(index, text, BORIS, "shared_and_personal")) == {
        shared.fragment_id,
        boris_own.fragment_id,
    }
    # Посторонний пользователь (и администратор — роли в фильтре нет) видит только общее.
    assert await _search(index, text, uuid4(), "shared_and_personal") == [shared.fragment_id]


async def test_shared_scope_excludes_even_own_personal(index: QdrantVectorIndex) -> None:
    shared = _point("регламент резервного копирования", "shared", BORIS)
    own = _point("регламент резервного копирования личный", "personal", ANN)
    await _store(index, shared, own)
    assert await _search(index, "регламент копирования", ANN, "shared") == [shared.fragment_id]


async def test_cogis_scope_returns_only_marked_shared_documents(index: QdrantVectorIndex) -> None:
    cogis = _point("плагин интерфейс IServerPlugin", "shared", BORIS, is_cogis=True)
    plain = _point("плагин интерфейс IServerPlugin описание", "shared", BORIS)
    own = _point("плагин интерфейс IServerPlugin черновик", "personal", ANN)
    await _store(index, cogis, plain, own)
    assert await _search(index, "плагин IServerPlugin", ANN, "cogis") == [cogis.fragment_id]


@pytest.mark.parametrize("branch", ["dense", "lexical"])
async def test_filter_guards_each_search_branch(index: QdrantVectorIndex, branch: str) -> None:
    """Чужой личный фрагмент не проходит ни векторной, ни лексической ветвью.

    Запрос совпадает с ним только в одной ветви. Что фильтр стоит в каждом `prefetch` и
    в корне одновременно, проверяет следующий тест: движок закрывает утечку любым из них.
    """
    secret = "уникальноеслово 50:21:0030210:417"
    foreign = _point(secret, "personal", BORIS)
    await _store(index, foreign, _point("общий документ о другом", "shared", BORIS))
    dense = replies.embedding(secret) or []
    lexical = sparse_vector(secret)
    if branch == "dense":
        lexical = SparseVector((), ())
    else:
        dense = replies.embedding("совсем иной текст") or []
    found = await index.search(dense, lexical, ANN, "shared_and_personal", 8)
    assert foreign.fragment_id not in found
    assert foreign.fragment_id in await index.search(
        dense, lexical, BORIS, "shared_and_personal", 8
    )


async def _captured_requests(
    index: QdrantVectorIndex,
    client: AsyncQdrantClient,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    limit: int = 5,
) -> list[models.QueryRequest]:
    captured: list[models.QueryRequest] = []
    original = client.query_batch_points

    async def spy(collection: str, requests: Any, **kwargs: Any) -> Any:
        captured.extend(requests)
        return await original(collection, requests, **kwargs)

    monkeypatch.setattr(client, "query_batch_points", spy)
    # Иного пути к точкам коллекции у поиска нет.
    monkeypatch.setattr(client, "query_points", None)
    await _search(index, query, ANN, "shared_and_personal", limit)
    return captured


async def test_every_request_of_the_batch_carries_access_filter(
    index: QdrantVectorIndex, client: AsyncQdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фильтр доступа — в обоих запросах пачки: в каждом `prefetch` и в корне (§10.4)."""
    exact, hybrid = await _captured_requests(index, client, monkeypatch, "договор 14-А")
    expected = access_filter(ANN, "shared_and_personal")

    assert exact.prefetch is None
    assert (exact.using, exact.limit, exact.with_payload) == ("lexical", 3, False)
    assert isinstance(exact.query, models.SparseVector)
    assert exact.filter == expected

    assert isinstance(hybrid.prefetch, list)
    assert [prefetch.using for prefetch in hybrid.prefetch] == ["dense", "lexical"]
    assert all(prefetch.filter == expected for prefetch in hybrid.prefetch)
    assert all(prefetch.limit == 40 for prefetch in hybrid.prefetch)
    assert hybrid.filter == expected
    assert hybrid.query == models.FusionQuery(fusion=models.Fusion.RRF)
    assert (hybrid.limit, hybrid.with_payload) == (5, False)


async def test_question_without_words_goes_as_one_filtered_dense_request(
    index: QdrantVectorIndex, client: AsyncQdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    (only,) = await _captured_requests(index, client, monkeypatch, "?! …")
    assert (only.using, only.prefetch, only.limit) == ("dense", None, 5)
    assert only.filter == access_filter(ANN, "shared_and_personal")


async def test_lexical_only_request_does_not_leak_foreign_personal(
    index: QdrantVectorIndex, client: AsyncQdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Лексический запрос пачки сам по себе: чужой личный фрагмент не проходит и через него."""
    secret = "уникальноеслово 50:21:0030210:417"
    foreign = _point(secret, "personal", BORIS)
    await _store(index, foreign)
    original = client.query_batch_points

    async def lexical_only(collection: str, requests: Any, **kwargs: Any) -> Any:
        return await original(collection, requests[:1], **kwargs)

    monkeypatch.setattr(client, "query_batch_points", lexical_only)
    assert await _search(index, secret, ANN, "shared_and_personal") == []
    assert await _search(index, secret, BORIS, "shared_and_personal") == [foreign.fragment_id]


async def test_best_lexical_matches_come_first_then_hybrid_without_repeats(
    index: QdrantVectorIndex,
) -> None:
    """Точное совпадение получает место в выдаче, даже если векторная ветвь его не видит.

    Сорок однотипных фрагментов ближе к вопросу по вектору, чем нужный: в ветвь `dense`
    (40 кандидатов) он не входит, и по одному слиянию RRF проиграл бы каждому из них.
    """
    boilerplate = "выписка из единого государственного реестра недвижимости правообладатель"
    crowd = [_point(f"{boilerplate} участок {n}", "shared", ANN) for n in range(45)]
    target = FragmentPoint(
        fragment_id=uuid4(),
        document_id=uuid4(),
        scope="shared",
        owner_id=ANN,
        is_cogis=False,
        dense=replies.embedding("совсем другой текст о плагинах") or [],
        lexical=sparse_vector("правообладатель Соколова Марина"),
    )
    await _store(index, *crowd, target)
    query = f"{boilerplate} Соколовой"
    found = await index.search(
        replies.embedding(query) or [], sparse_vector(query), ANN, "shared", 8
    )
    assert found[0] == target.fragment_id
    assert len(found) == len(set(found)) == 8


async def test_limit_caps_merged_result(index: QdrantVectorIndex) -> None:
    points = [_point(f"договор аренды участка {n}", "shared", ANN) for n in range(12)]
    await _store(index, *points)
    for limit in (1, 2, 8):
        found = await _search(index, "договор аренды участка 3", ANN, "shared", limit)
        assert len(found) == len(set(found)) == limit


def test_access_filter_matches_contract_shapes() -> None:
    user = UUID("11111111-1111-4111-8111-111111111111")
    dump = access_filter(user, "shared_and_personal").model_dump(exclude_none=True)
    shared = {"key": "scope", "match": {"value": "shared"}}
    own = {
        "must": [
            {"key": "scope", "match": {"value": "personal"}},
            {"key": "owner_id", "match": {"value": str(user)}},
        ]
    }
    assert dump == {"must": [{"should": [shared, own]}]}
    assert access_filter(user, "shared").model_dump(exclude_none=True) == {"must": [shared]}
    assert access_filter(user, "cogis").model_dump(exclude_none=True) == {
        "must": [shared, {"key": "is_cogis", "match": {"value": True}}]
    }


def test_unknown_scope_is_an_error_not_an_unfiltered_query() -> None:
    with pytest.raises(AssertionError):
        access_filter(ANN, "all")  # type: ignore[arg-type]


async def test_exact_number_ranks_first_among_similar_texts(index: QdrantVectorIndex) -> None:
    """Кадастровый номер находится по точному совпадению (ПД §5.2)."""
    target = _point("Выписка: кадастровый номер 77:01:0004012:345, площадь 2 318", "shared", ANN)
    others = [
        _point(f"Выписка: кадастровый номер 77:01:0004012:{n}, площадь 2 318", "shared", ANN)
        for n in (344, 346, 3450, 245)
    ]
    await _store(index, *others, target)
    found = await _search(index, "кому принадлежит участок 77:01:0004012:345", ANN, "shared")
    assert found[0] == target.fragment_id


async def test_replace_document_removes_previous_points(
    index: QdrantVectorIndex, client: AsyncQdrantClient
) -> None:
    document = uuid4()
    old = [_point(f"старый текст {n}", "shared", ANN, document=document) for n in range(5)]
    await index.replace_document(document, old)
    new = _point("новый текст", "shared", ANN, document=document)
    await index.replace_document(document, [new])
    assert await _search(index, "старый новый текст", ANN, "shared") == [new.fragment_id]
    await index.delete_document(document)
    await index.delete_document(document)  # повтор — не ошибка
    assert await _search(index, "новый текст", ANN, "shared") == []


async def test_points_are_written_in_configured_batches(
    index: QdrantVectorIndex, client: AsyncQdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []
    original = client.upsert

    async def spy(collection: str, points: list[Any], **kwargs: Any) -> Any:
        sizes.append(len(points))
        return await original(collection, points, **kwargs)

    monkeypatch.setattr(client, "upsert", spy)
    document = uuid4()
    points = [_point(f"текст {n}", "shared", ANN, document=document) for n in range(300)]
    await index.replace_document(document, points)
    assert sizes == [128, 128, 44]


async def test_point_payload_has_no_text_or_title(
    index: QdrantVectorIndex, client: AsyncQdrantClient
) -> None:
    point = _point("содержимое личного документа", "personal", ANN)
    await _store(index, point)
    stored = (await client.retrieve(index._collection, [str(point.fragment_id)]))[0]
    assert stored.payload == {
        "document_id": str(point.document_id),
        "scope": "personal",
        "owner_id": str(ANN),
        "is_cogis": False,
    }


async def test_ensure_collection_is_idempotent_and_refuses_other_parameters(
    client: AsyncQdrantClient, index: QdrantVectorIndex, settings: Settings
) -> None:
    await index.ensure_collection()
    params = (await client.get_collection(index._collection)).config.params
    assert isinstance(params.vectors, dict)
    assert (params.vectors["dense"].size, params.vectors["dense"].distance) == (1024, "Cosine")
    assert params.sparse_vectors is not None
    assert params.sparse_vectors["lexical"].modifier == models.Modifier.IDF

    embeddings = settings.kb.embeddings.model_copy(update={"dimension": 768})
    other = settings.kb.model_copy(update={"embeddings": embeddings})
    with pytest.raises(CollectionMismatchError):
        await QdrantVectorIndex(client, other, index._collection).ensure_collection()


async def test_missing_collection_is_reported_as_unavailable(
    client: AsyncQdrantClient, settings: Settings
) -> None:
    index = QdrantVectorIndex(client, settings.kb, f"kb_absent_{uuid4().hex}")
    with pytest.raises(VectorIndexUnavailableError):
        await _search(index, "вопрос", ANN, "shared")
    await index.drop_collection()  # отсутствие — не ошибка
