"""Диалоги: создание, список с курсором, переименование, удаление, изоляция (§5.2, §9.15)."""

from uuid import uuid4

import pytest

from tests.support import Portal, ask, new_dialog, upload

pytestmark = pytest.mark.anyio


async def test_create_and_read_dialog(portal: Portal) -> None:
    client = await portal.employee()
    response = await client.post("/api/dialogs", json={"kind": "chat"})
    assert response.status_code == 201
    dialog = response.json()
    assert dialog == {
        "id": dialog["id"],
        "kind": "chat",
        "title": None,
        "created_at": "2026-10-05T09:00:00Z",
        "updated_at": "2026-10-05T09:00:00Z",
    }
    assert (await client.get(f"/api/dialogs/{dialog['id']}")).json() == dialog


async def test_dialog_kinds_that_can_be_created(portal: Portal) -> None:
    client = await portal.employee()
    for kind in ("chat", "sql", "cogis"):
        response = await client.post("/api/dialogs", json={"kind": kind})
        assert response.status_code == 201 and response.json()["kind"] == kind
    # Диалог разбора создаётся только запуском разбора.
    for body in ({"kind": "docparse"}, {"kind": "other"}, {"kind": "chat", "title": "x"}, {}):
        response = await client.post("/api/dialogs", json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "validation_error"


async def test_list_is_ordered_by_update_and_paged_by_cursor(portal: Portal) -> None:
    client = await portal.employee()
    created = []
    for _ in range(5):
        portal.clock.advance(seconds=10)
        created.append(await new_dialog(client))
        await ask(client, created[-1])
    portal.clock.advance(seconds=10)
    await ask(client, created[1])  # новое сообщение поднимает диалог наверх

    first = (await client.get("/api/dialogs", params={"kind": "chat", "limit": 2})).json()
    assert [item["id"] for item in first["items"]] == [created[1], created[4]]
    second = (
        await client.get(
            "/api/dialogs", params={"kind": "chat", "limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()
    assert [item["id"] for item in second["items"]] == [created[3], created[2]]
    third = (
        await client.get(
            "/api/dialogs", params={"kind": "chat", "limit": 2, "cursor": second["next_cursor"]}
        )
    ).json()
    assert [item["id"] for item in third["items"]] == [created[0]]
    assert third["next_cursor"] is None

    everything = (await client.get("/api/dialogs", params={"kind": "chat"})).json()
    assert len(everything["items"]) == 5 and everything["next_cursor"] is None
    assert (await client.get("/api/dialogs", params={"kind": "sql"})).json()["items"] == []


async def test_list_parameters_are_validated(portal: Portal) -> None:
    client = await portal.employee()
    cases: list[tuple[dict[str, str | int], str]] = [
        ({}, "kind"),
        ({"kind": "other"}, "kind"),
        ({"kind": "chat", "limit": 0}, "limit"),
        ({"kind": "chat", "limit": 101}, "limit"),
        ({"kind": "chat", "cursor": "не курсор"}, "cursor"),
        ({"kind": "chat", "cursor": "WzFd"}, "cursor"),
    ]
    for params, field in cases:
        response = await client.get("/api/dialogs", params=params)
        assert response.status_code == 422, params
        assert response.json()["error"]["fields"][0]["field"] == field


async def test_rename_keeps_updated_at(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.clock.advance(minutes=5)
    renamed = await client.patch(f"/api/dialogs/{dialog_id}", json={"title": "  Аренда под ЛЭП "})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Аренда под ЛЭП"
    assert renamed.json()["updated_at"] == "2026-10-05T09:00:00Z"
    for title, code in (("  ", "required"), ("я" * 201, "too_long")):
        response = await client.patch(f"/api/dialogs/{dialog_id}", json={"title": title})
        assert response.status_code == 422
        assert [(f["field"], f["code"]) for f in response.json()["error"]["fields"]] == [
            ("title", code)
        ]


async def test_messages_are_paged_from_newest(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    for number in range(3):
        await ask(client, dialog_id, f"Вопрос {number}")
    first = (await client.get(f"/api/dialogs/{dialog_id}/messages", params={"limit": 4})).json()
    assert [(m["role"], m["content"]) for m in first["items"]] == [
        ("assistant", "Привет, мир."),
        ("user", "Вопрос 2"),
        ("assistant", "Привет, мир."),
        ("user", "Вопрос 1"),
    ]
    rest = (
        await client.get(
            f"/api/dialogs/{dialog_id}/messages", params={"cursor": first["next_cursor"]}
        )
    ).json()
    assert [m["content"] for m in rest["items"]] == ["Привет, мир.", "Вопрос 0"]
    assert rest["next_cursor"] is None
    question = rest["items"][1]
    assert set(question) == {
        "id", "role", "content", "status", "error_code", "reasoning", "reasoning_seconds",
        "attachments", "sources", "sources_found", "sql_check", "sql_dangers",
        "dropped_messages", "created_at",
    }  # fmt: skip
    assert question["status"] == "complete" and question["attachments"] == []
    assert question["sources"] is None and question["sources_found"] is None
    assert "params" not in question


async def test_delete_removes_dialog_messages_and_files(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    sent = (await upload(client, dialog_id, "a.txt", b"sent")).json()["id"]
    await ask(client, dialog_id, attachment_ids=[sent])
    await upload(client, dialog_id, "b.txt", b"unsent")
    root = portal.container.settings.files.root / "attachments"
    keys = [row.storage_key for row in await portal.rows("SELECT storage_key FROM attachments")]
    assert len(keys) == 2 and all((root.parent / key).exists() for key in keys)

    assert (await client.delete(f"/api/dialogs/{dialog_id}")).status_code == 204
    assert (await client.get(f"/api/dialogs/{dialog_id}")).status_code == 404
    assert not any((root.parent / key).exists() for key in keys)
    for table in ("dialogs", "messages", "attachments"):
        assert await portal.rows(f"SELECT 1 FROM {table}") == []
    assert (await client.delete(f"/api/dialogs/{dialog_id}")).status_code == 404


async def test_foreign_dialog_is_not_found_even_for_admin(portal: Portal) -> None:
    """Чужой чат и чужое вложение — 404 для сотрудника и для администратора (критерий 6)."""
    owner = await portal.employee("ivanov")
    dialog_id = await new_dialog(owner)
    attachment = (await upload(owner, dialog_id, "договор.txt", b"secret text")).json()["id"]
    await ask(owner, dialog_id, "Секретный вопрос", attachment_ids=[attachment])
    spare = (await upload(owner, dialog_id, "ещё.txt", b"more")).json()["id"]

    stranger = await portal.employee("petrova")
    admin = portal.client()
    await portal.onboard(admin, "boss", role="admin")
    base = f"/api/dialogs/{dialog_id}"
    requests = [
        ("GET", base, None),
        ("PATCH", base, {"title": "Моё"}),
        ("DELETE", base, None),
        ("GET", f"{base}/messages", None),
        ("POST", f"{base}/messages", {"content": "?", "mode": "fast", "knowledge": "none"}),
        ("POST", f"{base}/regenerate", None),
        ("GET", f"{base}/attachments/{attachment}/file", None),
        ("DELETE", f"{base}/attachments/{spare}", None),
    ]
    for client in (stranger, admin):
        for method, path, body in requests:
            response = await client.request(method, path, json=body)
            assert response.status_code == 404, (method, path)
            assert response.json()["error"]["code"] == "not_found"
            assert "Секрет" not in response.text and "secret" not in response.text
        refused = await upload(client, dialog_id, "x.txt", b"x")
        assert refused.status_code == 404
        listed = (await client.get("/api/dialogs", params={"kind": "chat"})).json()
        assert listed["items"] == []
        # Чужое вложение не прикрепляется и к своему диалогу.
        own = await new_dialog(client)
        response = await client.post(
            f"/api/dialogs/{own}/messages",
            json={"content": "?", "mode": "fast", "knowledge": "none", "attachment_ids": [spare]},
        )
        assert response.status_code == 422
        assert (await client.get(f"/api/dialogs/{own}/attachments/{spare}/file")).status_code == 404

    assert (await owner.get(base)).status_code == 200
    assert len((await owner.get(f"{base}/messages")).json()["items"]) == 2
    assert len(await portal.rows("SELECT 1 FROM attachments")) == 2


async def test_unknown_and_malformed_ids_are_not_found(portal: Portal) -> None:
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    for path in (
        f"/api/dialogs/{uuid4()}",
        "/api/dialogs/not-a-uuid",
        f"/api/dialogs/{dialog_id}/attachments/{uuid4()}/file",
        f"/api/dialogs/{dialog_id}/attachments/not-a-uuid/file",
        f"/api/dialogs/{uuid4()}/export",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


async def test_dialog_routes_require_finished_login(portal: Portal) -> None:
    temporary = await portal.create_user("ivanov")
    async with portal.client() as anonymous, portal.client() as unfinished:
        await unfinished.post("/api/auth/login", json={"login": "ivanov", "password": temporary})
        for path in ("/api/dialogs?kind=chat", "/api/dialogs/not-a-uuid"):
            assert (await anonymous.get(path)).status_code == 401
            response = await unfinished.get(path)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "login_step_required"


async def test_empty_dialog_is_hidden_from_history_but_reachable(portal: Portal) -> None:
    client = await portal.employee()
    empty = await new_dialog(client)
    used = await new_dialog(client)
    await ask(client, used)
    listed = (await client.get("/api/dialogs", params={"kind": "chat"})).json()["items"]
    assert [item["id"] for item in listed] == [used]
    assert (await client.get(f"/api/dialogs/{empty}")).status_code == 200
    assert (await upload(client, empty, "a.txt", b"x")).status_code == 201
    assert (await client.get(f"/api/dialogs/{empty}/messages")).json()["items"] == []


async def test_abandoned_empty_dialogs_are_purged_after_ttl(portal: Portal) -> None:
    owner = await portal.employee("ivanov")
    other = await portal.employee("petrova")
    owner_id = await portal.user_id("ivanov")
    abandoned = await new_dialog(owner)
    await upload(owner, abandoned, "a.txt", b"x")
    foreign = await new_dialog(other)
    used = await new_dialog(owner)
    await ask(owner, used)
    files = portal.container.settings.files.root / "attachments"
    assert len(list(files.iterdir())) == 1

    async def remaining() -> set[str]:
        return {str(row.id) for row in await portal.rows("SELECT id FROM dialogs")}

    dialogs = portal.container.dialogs
    portal.clock.advance(hours=23)
    recent = str((await dialogs.create(owner_id, "chat")).id)
    assert await remaining() == {abandoned, foreign, used, recent}  # срок ещё не вышел

    # Новый диалог убирает брошенные пустые диалоги только у своего владельца.
    portal.clock.advance(hours=2)
    fresh = str((await dialogs.create(owner_id, "chat")).id)
    assert await remaining() == {foreign, used, recent, fresh}
    assert list(files.iterdir()) == []

    # При старте процесса — у всех; диалог с сообщениями не трогается никогда.
    portal.clock.advance(hours=30)
    await dialogs.purge_empty()
    assert await remaining() == {used}
