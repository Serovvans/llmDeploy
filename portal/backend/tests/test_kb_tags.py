"""«Министерства» и теги: права по текущей роли, приватность тегов (§8.1, §8.5, §14)."""

from typing import Any

import httpx
import pytest

from portal.kb.ports import VectorIndexUnavailableError
from portal.kb.tags import TagsError, normalize_tags, selected_keys
from tests.kb_support import KbBench, add_document, added
from tests.support import Portal

pytestmark = pytest.mark.anyio

DOCUMENTS = "/api/kb/documents"
TAGS = "/api/kb/tags"


async def _admin(portal: Portal, login: str = "boss") -> httpx.AsyncClient:
    client = portal.client()
    await portal.onboard(client, login, "admin")
    return client


async def _refusal(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _suggested(client: httpx.AsyncClient, **params: Any) -> list[str]:
    response = await client.get(TAGS, params=params)
    assert response.status_code == 200, response.text
    items: list[str] = response.json()["items"]
    return items


async def _tags_of(portal: Portal, document_id: str) -> list[tuple[str, str]]:
    rows = await portal.rows(
        "SELECT tag_key, tag FROM kb_document_tags WHERE document_id = :id ORDER BY tag_key",
        id=document_id,
    )
    return [(row.tag_key, row.tag) for row in rows]


# --- «Министерства» ---


async def test_employee_cannot_change_ministries_but_sees_them(bench: KbBench) -> None:
    """Сотрудник не добавляет, не удаляет, не обрабатывает повторно и не меняет теги."""
    portal = bench.portal
    employee = await portal.employee("ivanov")
    admin = await _admin(portal)

    refused = await add_document(employee, "приказ.txt", b"order", "ministries", tags=["Минстрой"])
    assert await _refusal(refused) == (403, "forbidden")
    assert await portal.rows("SELECT 1 FROM kb_documents") == []
    assert not (portal.container.settings.files.root / "kb").exists()  # файл не принимался

    document_id = await added(admin, "приказ.txt", b"order", "ministries", tags=["Минстрой"])
    await portal.execute("UPDATE kb_documents SET status = 'error', error_code = 'internal_error'")
    await portal.execute("DELETE FROM kb_jobs")
    base = f"{DOCUMENTS}/{document_id}"

    seen = await employee.get(base)
    assert (seen.json()["scope"], seen.json()["can_delete"]) == ("ministries", False)
    assert seen.json()["tags"] == ["Минстрой"]
    listing = await employee.get(DOCUMENTS, params={"scope": "ministries"})
    assert [item["id"] for item in listing.json()["items"]] == [document_id]
    assert (await employee.get(f"{base}/file")).content == b"order"
    for scope in ("shared", "personal"):
        other = await employee.get(DOCUMENTS, params={"scope": scope})
        assert other.json()["total"] == 0

    attempts = [
        await employee.delete(base),
        await employee.post(f"{base}/retry"),
        await employee.put(f"{base}/tags", json={"tags": []}),
    ]
    for response in attempts:
        assert await _refusal(response) == (403, "forbidden"), response.request.url
    assert await _tags_of(portal, document_id) == [("минстрой", "Минстрой")]
    state = await portal.rows("SELECT status, deleted_at FROM kb_documents")
    assert [tuple(row) for row in state] == [("error", None)]
    assert await portal.rows("SELECT 1 FROM kb_jobs") == []
    events = {event for event, _ in await portal.audit_events()}
    assert not events & {"document_deleted", "document_retried", "document_tags_changed"}


async def test_ministries_rights_follow_current_role_not_authorship(bench: KbBench) -> None:
    """Другой администратор распоряжается документом; бывший администратор — уже нет."""
    portal = bench.portal
    author = await _admin(portal, "boss")
    colleague = await _admin(portal, "chief")
    first = await added(author, "первый.txt", b"first", "ministries")
    second = await added(author, "второй.txt", b"second", "ministries")

    assert (await colleague.get(f"{DOCUMENTS}/{first}")).json()["can_delete"] is True
    retagged = await colleague.put(f"{DOCUMENTS}/{first}/tags", json={"tags": ["Приказы"]})
    assert retagged.status_code == 200, retagged.text
    assert (await colleague.delete(f"{DOCUMENTS}/{first}")).status_code == 204
    changes = [
        details
        for event, details in await portal.audit_events()
        if event in ("document_tags_changed", "document_deleted")
    ]
    assert changes == [{"document_id": first, "scope": "ministries", "by_admin": True}] * 2

    # Роль сменили, сессия осталась: право считается по роли на момент запроса.
    await portal.execute("UPDATE users SET role = 'employee' WHERE login = 'boss'")
    own = await author.get(f"{DOCUMENTS}/{second}")
    assert (own.json()["author"]["is_me"], own.json()["can_delete"]) == (True, False)
    assert await _refusal(await author.delete(f"{DOCUMENTS}/{second}")) == (403, "forbidden")
    assert await _refusal(await author.put(f"{DOCUMENTS}/{second}/tags", json={"tags": ["x"]})) == (
        403,
        "forbidden",
    )
    again = await add_document(author, "третий.txt", b"third", "ministries")
    assert await _refusal(again) == (403, "forbidden")


async def test_ministries_have_own_duplicate_check_and_no_cogis_mark(portal: Portal) -> None:
    admin = await _admin(portal)
    await added(admin, "приказ.txt", b"same", "shared")
    document_id = await added(admin, "приказ.txt", b"same", "ministries")
    repeated = await add_document(admin, "копия.txt", b"same", "ministries")
    assert await _refusal(repeated) == (409, "duplicate_document")
    assert repeated.json()["error"]["details"]["document"]["id"] == document_id
    marked = await add_document(admin, "sdk.txt", b"sdk", "ministries", is_cogis=True)
    assert await _refusal(marked) == (422, "validation_error")
    uploads = [d for event, d in await portal.audit_events() if event == "document_uploaded"]
    assert [d["scope"] for d in uploads] == ["shared", "ministries"]


# --- теги: правила ---


def test_tag_rules_merge_by_key_and_keep_first_spelling() -> None:
    tags = normalize_tags(["  Аренда ", "аренда", "МИНСТРОЙ"], max_count=2, max_length=8)
    assert [(tag.key, tag.text) for tag in tags] == [("аренда", "Аренда"), ("минстрой", "МИНСТРОЙ")]
    assert selected_keys([" Аренда", "аренда", "  ", "Б"]) == ["аренда", "б"]
    for raw, code in [
        (["а", "б", "в"], "too_many"),
        (["длинныйтег"], "too_long"),
        (["   "], "invalid_format"),
        (["две\nстроки"], "invalid_format"),
        (["таб\tуляция"], "invalid_format"),
        (["нуль\x00"], "invalid_format"),
        (["скр‮ыто"], "invalid_format"),
        (["сур\ud800"], "invalid_format"),
    ]:
        with pytest.raises(TagsError) as refused:
            normalize_tags(raw, max_count=2, max_length=8)
        assert refused.value.code == code, raw


async def test_upload_with_invalid_tags_creates_nothing(portal: Portal) -> None:
    client = await portal.employee()
    limits = portal.container.settings.kb.tags
    cases = [
        ([f"тег {n}" for n in range(limits.max_per_document + 1)], "too_many"),
        (["я" * (limits.max_length + 1)], "too_long"),
        (["хороший", " "], "invalid_format"),
    ]
    for tags, code in cases:
        response = await add_document(client, "a.txt", b"text", tags=tags)
        assert response.status_code == 422, response.text
        fields = response.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [("tags", code)]
    # Сколько бы полей тегов ни пришло, отказ — по полю `tags`, а не «запрос не разобран».
    for count in (limits.max_per_document + 2, 30):
        response = await add_document(
            client, "a.txt", b"text", tags=[f"тег {n}" for n in range(count)]
        )
        assert response.status_code == 422, response.text
        fields = response.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [("tags", "too_many")]
    assert await portal.rows("SELECT 1 FROM kb_documents") == []
    assert await portal.rows("SELECT 1 FROM kb_document_tags") == []
    assert not (portal.container.settings.files.root / "kb").exists()  # файл не принимался
    edge = await add_document(client, "a.txt", b"text", tags=["я" * limits.max_length] * 3)
    assert edge.status_code == 201 and edge.json()["tags"] == ["я" * limits.max_length]


async def test_tags_are_replaced_as_a_whole_by_those_who_manage_the_document(
    bench: KbBench,
) -> None:
    portal = bench.portal
    author = await portal.employee("ivanov")
    other = await portal.employee("petrov")
    admin = await _admin(portal)
    document_id = await added(author, "договор.txt", b"lease", tags=["Минстрой", "аренда"])
    path = f"{DOCUMENTS}/{document_id}/tags"
    assert (await other.get(f"{DOCUMENTS}/{document_id}")).json()["tags"] == ["аренда", "Минстрой"]

    assert await _refusal(await other.put(path, json={"tags": ["чужой"]})) == (403, "forbidden")
    for body, expected in [
        ({"tags": ["Аренда", "Срочно"]}, ["Аренда", "Срочно"]),
        ({"tags": []}, []),
    ]:
        response = await author.put(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == expected
    by_admin = await admin.put(path, json={"tags": ["Проверено"]})
    assert by_admin.json()["tags"] == ["Проверено"]
    assert await _tags_of(portal, document_id) == [("проверено", "Проверено")]

    unknown_field = {"tags": ["ok"], "unknown": 1}
    for body in ({"tags": "аренда"}, {"tags": [1]}, {}, {"tags": ["ok", "\n"]}, unknown_field):
        assert await _refusal(await author.put(path, json=body)) == (422, "validation_error")
    assert await _tags_of(portal, document_id) == [("проверено", "Проверено")]
    # Состояние документа правка не трогает и заданий не создаёт: переиндексации нет.
    jobs = await portal.rows("SELECT kind FROM kb_jobs")
    assert [job.kind for job in jobs] == ["index"]
    changes = [d for event, d in await portal.audit_events() if event == "document_tags_changed"]
    assert changes == [
        {"document_id": document_id, "scope": "shared", "by_admin": False},
        {"document_id": document_id, "scope": "shared", "by_admin": False},
        {"document_id": document_id, "scope": "shared", "by_admin": True},
    ]
    rows = await portal.rows("SELECT details::text AS details FROM audit_log")
    assert not any("Проверено" in row.details or "Срочно" in row.details for row in rows)


async def test_tags_do_not_change_when_qdrant_is_unavailable(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "договор.txt", b"lease", tags=["Аренда"])

    async def unavailable(*args: Any, **kwargs: Any) -> None:
        raise VectorIndexUnavailableError

    monkeypatch.setattr("portal.kb.qdrant.QdrantVectorIndex.set_tags", unavailable)
    response = await client.put(f"{DOCUMENTS}/{document_id}/tags", json={"tags": ["Новый"]})
    assert await _refusal(response) == (503, "service_unavailable")
    assert await _tags_of(portal, document_id) == [("аренда", "Аренда")]
    events = [event for event, _ in await portal.audit_events()]
    assert "document_tags_changed" not in events


# --- теги: приватность ---


@pytest.mark.parametrize("stranger_role", ["employee", "admin"])
async def test_foreign_personal_tags_are_invisible_everywhere(
    bench: KbBench, stranger_role: str
) -> None:
    """Тег, который стоит только на чужих личных документах, не появляется нигде (§8.5)."""
    portal = bench.portal
    owner = await portal.employee("ivanov")
    stranger = portal.client()
    await portal.onboard(stranger, "stranger", stranger_role)
    secret = await added(owner, "личное.txt", b"private", "personal", tags=["Тайна", "Аренда"])
    await added(owner, "общее.txt", b"shared", tags=["аренда", "Общий"])

    assert [tag.lower() for tag in await _suggested(owner)] == ["аренда", "общий", "тайна"]
    assert await _suggested(owner, scope="personal") == ["Аренда", "Тайна"]
    # Постороннему — только теги видимых ему документов и их написание.
    for params in ({}, {"scope": ["shared", "personal", "ministries"]}, {"q": "тай"}):
        suggested = await _suggested(stranger, **params)
        assert suggested in (["аренда", "Общий"], []), params
        assert "Тайна" not in suggested and "Аренда" not in suggested
    assert await _suggested(stranger, scope="personal") == []

    by_tag = await stranger.get(DOCUMENTS, params={"scope": "personal", "tags": "Тайна"})
    assert by_tag.json() == {"items": [], "page": 1, "page_size": 50, "total": 0}
    for scope in ("shared", "ministries"):
        hidden = await stranger.get(DOCUMENTS, params={"scope": scope, "tags": "тайна"})
        assert (hidden.status_code, hidden.json()["total"]) == (200, 0)
    put = await stranger.put(f"{DOCUMENTS}/{secret}/tags", json={"tags": ["Взлом"]})
    assert await _refusal(put) == (404, "not_found")
    assert "Тайна" not in put.text
    assert await _tags_of(portal, secret) == [("аренда", "Аренда"), ("тайна", "Тайна")]
    # Собственный тег постороннего с тем же ключом чужого написания не раскрывает.
    await added(stranger, "моё.txt", b"mine", "personal", tags=["ТАЙНА"])
    assert await _suggested(stranger, q="тайн") == ["ТАЙНА"]


async def test_suggestions_and_list_filter_by_tags(portal: Portal) -> None:
    client = await portal.employee("ivanov")
    admin = await _admin(portal)
    lease = await added(client, "аренда.txt", b"1", tags=["Аренда", "100%"])
    sale = await added(client, "купля.txt", b"2", tags=["Купля"])
    await added(client, "прочее.txt", b"3")
    order = await added(admin, "приказ.txt", b"4", "ministries", tags=["Приказы", "Аренда"])
    gone = await added(client, "удалённый.txt", b"5", tags=["Удалённое"])
    assert (await client.delete(f"{DOCUMENTS}/{gone}")).status_code == 204

    assert await _suggested(client) == ["100%", "Аренда", "Купля", "Приказы"]
    assert await _suggested(client, scope="ministries") == ["Аренда", "Приказы"]
    assert await _suggested(client, q="РЕН") == ["Аренда"]
    assert await _suggested(client, q="%") == ["100%"]  # знак подстановки — обычный символ
    assert await _suggested(client, q="_") == []
    assert await _suggested(client, q="а\x00") == []
    assert (await client.get(TAGS, params={"scope": "all"})).status_code == 422

    async def listed(scope: str, *tags: str) -> set[str]:
        response = await client.get(DOCUMENTS, params={"scope": scope, "tags": list(tags)})
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    assert await listed("shared", "аренда") == {lease}
    assert await listed("shared", "АРЕНДА", "купля") == {lease, sale}
    assert await listed("ministries", "Аренда") == {order}
    assert await listed("shared", "нет такого") == set()
    assert len(await listed("shared")) == 3

    limits = portal.container.settings.kb.tags
    for tags, code in [
        (["я" * (limits.max_length + 1)], "too_long"),
        ([f"т{n}" for n in range(limits.max_selected + 1)], "too_many"),
        ([" "], "invalid_format"),
    ]:
        response = await client.get(DOCUMENTS, params={"scope": "shared", "tags": tags})
        fields = response.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [("tags", code)]


async def test_suggestions_are_limited_by_configuration(portal: Portal) -> None:
    client = await portal.employee()
    limit = portal.container.settings.kb.tags.suggest_limit
    await portal.execute(
        "INSERT INTO kb_document_tags (document_id, tag_key, tag) "
        "SELECT :id, 'тег ' || n, 'Тег ' || n FROM generate_series(1, :count) AS n",
        id=await added(client, "много.txt", b"many"),
        count=limit + 5,
    )
    assert len(await _suggested(client)) == limit
