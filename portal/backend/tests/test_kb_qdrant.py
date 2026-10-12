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
    KnowledgeSelection,
    SparseVector,
    VectorIndexUnavailableError,
)
from portal.kb.qdrant import QdrantVectorIndex, access_filter
from tests.kb_support import ALL_COLLECTIONS, COGIS, SHARED
from tests.kb_support import SHARED_AND_PERSONAL as BOTH

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
    text: str,
    scope: str,
    owner: UUID,
    *,
    is_cogis: bool = False,
    document: UUID | None = None,
    tags: tuple[str, ...] = (),
) -> FragmentPoint:
    return FragmentPoint(
        fragment_id=uuid4(),
        document_id=document or uuid4(),
        scope=scope,  # type: ignore[arg-type]
        owner_id=owner,
        is_cogis=is_cogis,
        tags=tags,
        dense=replies.embedding(text) or [],
        lexical=sparse_vector(text),
    )


async def _search(
    index: QdrantVectorIndex,
    query: str,
    user: UUID,
    selection: KnowledgeSelection,
    limit: int = 8,
) -> list[UUID]:
    """Выдача, как её собирает поиск: лексические совпадения, затем слияние ветвей."""
    hits = await index.search(
        replies.embedding(query) or [], sparse_vector(query), user, selection, limit
    )
    return hits.ordered()[:limit]


async def _store(index: QdrantVectorIndex, *points: FragmentPoint) -> None:
    await _store_document(index, *points)


async def _store_document(index: QdrantVectorIndex, *points: FragmentPoint) -> None:
    """Записать точки и ввести их в действие — по одной, каждая со своими тегами."""
    for point in points:
        await index.stage_document(point.document_id, [point])
        await index.publish_document(point.document_id, point.tags)


async def test_foreign_personal_fragment_is_never_returned(index: QdrantVectorIndex) -> None:
    text = "секретный договор аренды участка 77:01:0004012:345"
    shared = _point(text, "shared", BORIS)
    ann_own = _point(text, "personal", ANN)
    boris_own = _point(text, "personal", BORIS)
    await _store(index, shared, ann_own, boris_own)

    found = await _search(index, text, ANN, BOTH)
    assert set(found) == {shared.fragment_id, ann_own.fragment_id}
    assert set(await _search(index, text, BORIS, BOTH)) == {
        shared.fragment_id,
        boris_own.fragment_id,
    }
    # Посторонний пользователь (и администратор — роли в фильтре нет) видит только общее.
    assert await _search(index, text, uuid4(), BOTH) == [shared.fragment_id]


async def test_shared_scope_excludes_even_own_personal(index: QdrantVectorIndex) -> None:
    shared = _point("регламент резервного копирования", "shared", BORIS)
    own = _point("регламент резервного копирования личный", "personal", ANN)
    await _store(index, shared, own)
    assert await _search(index, "регламент копирования", ANN, SHARED) == [shared.fragment_id]


async def test_cogis_scope_returns_only_marked_shared_documents(index: QdrantVectorIndex) -> None:
    cogis = _point("плагин интерфейс IServerPlugin", "shared", BORIS, is_cogis=True)
    plain = _point("плагин интерфейс IServerPlugin описание", "shared", BORIS)
    own = _point("плагин интерфейс IServerPlugin черновик", "personal", ANN)
    await _store(index, cogis, plain, own)
    assert await _search(index, "плагин IServerPlugin", ANN, COGIS) == [cogis.fragment_id]


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
    found = await index.search(dense, lexical, ANN, ALL_COLLECTIONS, 8)
    assert foreign.fragment_id not in found.ordered()
    theirs = await index.search(dense, lexical, BORIS, ALL_COLLECTIONS, 8)
    assert foreign.fragment_id in theirs.ordered()


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
    await _search(index, query, ANN, BOTH, limit)
    return captured


async def test_every_request_of_the_batch_carries_access_filter(
    index: QdrantVectorIndex, client: AsyncQdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фильтр доступа — в обоих запросах пачки: в каждом `prefetch` и в корне (§10.4)."""
    exact, hybrid = await _captured_requests(index, client, monkeypatch, "договор 14-А")
    expected = access_filter(ANN, BOTH)

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
    assert only.filter == access_filter(ANN, BOTH)


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
    assert await _search(index, secret, ANN, BOTH) == []
    assert await _search(index, secret, BORIS, BOTH) == [foreign.fragment_id]


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
        tags=(),
        dense=replies.embedding("совсем другой текст о плагинах") or [],
        lexical=sparse_vector("правообладатель Соколова Марина"),
    )
    await _store(index, *crowd, target)
    query = f"{boilerplate} Соколовой"
    hits = await index.search(replies.embedding(query) or [], sparse_vector(query), ANN, SHARED, 8)
    assert hits.hybrid[0] != target.fragment_id  # одно слияние RRF первым его не ставит
    found = hits.ordered()
    assert found[0] == target.fragment_id
    assert len(found) == len(set(found)) >= 8


async def test_limits_cap_both_candidate_lists(
    index: QdrantVectorIndex, settings: Settings
) -> None:
    points = [_point(f"договор аренды участка {n}", "shared", ANN) for n in range(12)]
    await _store(index, *points)
    query = "договор аренды участка 3"
    for limit in (1, 2, 8):
        hits = await index.search(
            replies.embedding(query) or [], sparse_vector(query), ANN, SHARED, limit
        )
        assert len(hits.hybrid) == len(set(hits.hybrid)) == limit
        assert len(hits.lexical) == settings.kb.search.lexical_slots


def test_access_filter_matches_contract_shapes() -> None:
    user = UUID("11111111-1111-4111-8111-111111111111")
    shared = {"key": "scope", "match": {"value": "shared"}}
    ministries = {"key": "scope", "match": {"value": "ministries"}}
    own = {
        "must": [
            {"key": "scope", "match": {"value": "personal"}},
            {"key": "owner_id", "match": {"value": str(user)}},
        ]
    }
    not_pending = [{"key": "pending", "match": {"value": True}}]
    full = KnowledgeSelection(ALL_COLLECTIONS.collections, frozenset({" Аренда", "МИНСТРОЙ"}))
    assert access_filter(user, full).model_dump(exclude_none=True) == {
        "must": [
            {"should": [shared, ministries, own]},
            {"key": "tags", "match": {"any": ["аренда", "минстрой"]}},
        ],
        "must_not": not_pending,
    }
    assert access_filter(user, SHARED).model_dump(exclude_none=True) == {
        "must": [{"should": [shared]}],
        "must_not": not_pending,
    }
    only_mine = KnowledgeSelection(frozenset({"personal"}))
    assert access_filter(user, only_mine).model_dump(exclude_none=True) == {
        "must": [{"should": [own]}],
        "must_not": not_pending,
    }
    assert access_filter(user, COGIS).model_dump(exclude_none=True) == {
        "must": [shared, {"key": "is_cogis", "match": {"value": True}}],
        "must_not": not_pending,
    }


def test_selection_cannot_widen_access_or_skip_a_branch() -> None:
    """Пустой набор, неизвестная коллекция и CoGIS вне общей базы — ошибка, а не поиск."""
    for collections in (frozenset(), frozenset({"all"}), frozenset({"shared", "foreign"})):
        with pytest.raises(ValueError, match="набор коллекций"):
            KnowledgeSelection(collections)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="CoGIS"):
        KnowledgeSelection(frozenset({"shared", "personal"}), cogis_only=True)
    with pytest.raises(ValueError, match="CoGIS"):
        KnowledgeSelection(frozenset({"shared"}), frozenset({"тег"}), cogis_only=True)


async def test_ministries_are_searched_by_everyone_only_when_selected(
    index: QdrantVectorIndex,
) -> None:
    ministry = _point("приказ министерства о кадастровой оценке", "ministries", BORIS)
    shared = _point("приказ о кадастровой оценке общий", "shared", BORIS)
    await _store(index, ministry, shared)
    query = "приказ о кадастровой оценке"
    only = KnowledgeSelection(frozenset({"ministries"}))
    assert await _search(index, query, ANN, only) == [ministry.fragment_id]
    assert await _search(index, query, ANN, SHARED) == [shared.fragment_id]
    assert set(await _search(index, query, ANN, ALL_COLLECTIONS)) == {
        ministry.fragment_id,
        shared.fragment_id,
    }
    assert await _search(index, query, ANN, COGIS) == []


async def test_tags_only_narrow_and_never_open_foreign_personal(index: QdrantVectorIndex) -> None:
    """Тег сужает выбранные коллекции; чужой личный документ с тем же тегом не находится."""
    text = "договор аренды земельного участка"
    tagged = _point(text, "shared", BORIS, tags=("аренда",))
    other = _point(f"{text} иной", "shared", BORIS, tags=("купля",))
    mine = _point(f"{text} мой", "personal", ANN, tags=("аренда", "черновик"))
    foreign = _point(f"{text} чужой", "personal", BORIS, tags=("аренда",))
    await _store(index, tagged, other, mine, foreign)
    # Точка, записанная до 0.15: поля `tags` нет вовсе — под условие по тегам не подходит.
    legacy = _point(f"{text} прежний", "shared", BORIS)
    await index._client.upsert(
        index._collection,
        [
            models.PointStruct(
                id=str(legacy.fragment_id),
                vector={"dense": list(legacy.dense)},
                payload={"document_id": str(legacy.document_id), "scope": "shared"},
            )
        ],
    )

    def by_tag(*tags: str) -> KnowledgeSelection:
        return KnowledgeSelection(ALL_COLLECTIONS.collections, frozenset(tags))

    assert set(await _search(index, text, ANN, by_tag("Аренда"))) == {
        tagged.fragment_id,
        mine.fragment_id,
    }
    assert set(await _search(index, text, ANN, by_tag("купля", "черновик"))) == {
        other.fragment_id,
        mine.fragment_id,
    }
    assert await _search(index, text, ANN, by_tag("нет такого")) == []
    shared_only = KnowledgeSelection(frozenset({"shared"}), frozenset({"аренда"}))
    assert await _search(index, text, ANN, shared_only) == [tagged.fragment_id]
    assert legacy.fragment_id in await _search(index, text, ANN, SHARED)


async def test_set_tags_changes_all_points_of_document_without_rewriting_them(
    index: QdrantVectorIndex, client: AsyncQdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = uuid4()
    points = [_point(f"регламент часть {n}", "shared", ANN, document=document) for n in range(3)]
    bystander = _point("регламент иной", "shared", ANN, tags=("архив",))
    await _store(index, *points, bystander)
    monkeypatch.setattr(client, "upsert", None)  # правка тегов точки не перезаписывает
    by_tag = KnowledgeSelection(frozenset({"shared"}), frozenset({"регламенты"}))
    assert await _search(index, "регламент", ANN, by_tag) == []
    await index.set_tags(document, ["регламенты"])
    assert set(await _search(index, "регламент", ANN, by_tag)) == {p.fragment_id for p in points}
    await index.set_tags(document, [])
    assert await _search(index, "регламент", ANN, by_tag) == []
    archive = KnowledgeSelection(frozenset({"shared"}), frozenset({"архив"}))
    assert await _search(index, "регламент", ANN, archive) == [bystander.fragment_id]


async def test_pending_points_are_invisible_to_search(
    index: QdrantVectorIndex, client: AsyncQdrantClient
) -> None:
    live = _point("устав общества действующий", "shared", ANN)
    staged = _point("устав общества запас", "shared", ANN)
    await _store(index, live, staged)
    await client.set_payload(index._collection, {"pending": True}, [str(staged.fragment_id)])
    assert await _search(index, "устав общества", ANN, ALL_COLLECTIONS) == [live.fragment_id]


async def test_exact_number_ranks_first_among_similar_texts(index: QdrantVectorIndex) -> None:
    """Кадастровый номер находится по точному совпадению (ПД §5.2)."""
    target = _point("Выписка: кадастровый номер 77:01:0004012:345, площадь 2 318", "shared", ANN)
    others = [
        _point(f"Выписка: кадастровый номер 77:01:0004012:{n}, площадь 2 318", "shared", ANN)
        for n in (344, 346, 3450, 245)
    ]
    await _store(index, *others, target)
    found = await _search(index, "кому принадлежит участок 77:01:0004012:345", ANN, SHARED)
    assert found[0] == target.fragment_id


async def test_staged_points_replace_previous_ones_only_after_publish_and_prune(
    index: QdrantVectorIndex,
) -> None:
    """Порядок §10.6 на уровне порта: запас невидим, уборка оставляет только названное."""
    document = uuid4()
    old = [_point(f"старый текст {n}", "shared", ANN, document=document) for n in range(5)]
    await _store(index, *old)
    bystander = _point("старый текст соседа", "shared", ANN)
    await _store(index, bystander)
    old_ids = {point.fragment_id for point in old}
    new = _point("новый текст", "shared", ANN, document=document, tags=("устаревший",))
    await index.stage_document(document, [new])
    found = set(await _search(index, "старый новый текст", ANN, SHARED))
    assert found == old_ids | {bystander.fragment_id}
    # Уборка по прежним фрагментам стирает запас прерванной попытки и ничего больше.
    await index.prune_document(document, [point.fragment_id for point in old])
    await index.stage_document(document, [new])
    await index.publish_document(document, ["договоры"])
    by_tag = KnowledgeSelection(frozenset({"shared"}), frozenset({"договоры"}))
    assert await _search(index, "новый текст", ANN, by_tag) == [new.fragment_id]
    await index.prune_document(document, [new.fragment_id])
    found = set(await _search(index, "старый новый текст", ANN, SHARED))
    assert found == {new.fragment_id, bystander.fragment_id}
    await index.delete_document(document)
    await index.delete_document(document)  # повтор — не ошибка
    assert await _search(index, "новый текст", ANN, SHARED) == [bystander.fragment_id]


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
    await index.stage_document(document, points)
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
        "tags": [],
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
        await _search(index, "вопрос", ANN, SHARED)
    await index.drop_collection()  # отсутствие — не ошибка
