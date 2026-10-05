"""Маршруты базы знаний: добавление, список, повтор файла, права, изоляция (§8, §14)."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from portal.core.settings import Settings
from portal.files.names import display_file_name as display_title
from tests import samples
from tests.conftest import close_portal, make_portal
from tests.kb_support import KbBench, add_document, added, kb_settings
from tests.support import Portal

pytestmark = pytest.mark.anyio

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_PDF = samples.text_pdf([samples.TEXT_LAYER, "Second page with enough text layer inside"])


async def _document(client: httpx.AsyncClient, document_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/kb/documents/{document_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- добавление ---


@pytest.mark.parametrize(
    ("name", "content", "media_type", "page_count"),
    [
        ("договор.pdf", TEXT_PDF, "application/pdf", 2),
        ("скан.pdf", samples.scan_pdf(3), "application/pdf", 3),
        ("регламент.docx", samples.docx_file("Регламент", "Пункт первый"), DOCX_TYPE, None),
        ("заметка.txt", "Текст заметки".encode(), "text/plain", None),
        ("памятка.md", "# Памятка\n\nтекст".encode("cp1251"), "text/markdown", None),
        ("фото.png", samples.png(), "image/png", 1),
        ("фото.jpg", samples.jpeg(), "image/jpeg", 1),
    ],
)
async def test_upload_accepts_each_format_and_queues_indexing(
    portal: Portal, name: str, content: bytes, media_type: str, page_count: int | None
) -> None:
    client = await portal.employee()
    response = await add_document(client, name, content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "id": body["id"],
        "title": name,
        "scope": "shared",
        "is_cogis": False,
        "author": {"full_name": "Пользователь ivanov", "is_me": True},
        "created_at": "2026-10-05T09:00:00Z",
        "page_count": page_count,
        "status": "queued",
        "error_code": None,
        "progress": None,
        "can_delete": True,
    }
    row = (await portal.rows("SELECT * FROM kb_documents"))[0]
    assert (row.media_type, row.size_bytes) == (media_type, len(content))
    assert bytes(row.sha256) == hashlib.sha256(content).digest()
    # Имя на диске генерирует сервер: `kb/<uuid>`, от имени файла не зависит.
    assert row.storage_key.startswith("kb/") and name not in row.storage_key
    assert (portal.container.settings.files.root / row.storage_key).read_bytes() == content
    jobs = await portal.rows("SELECT kind, attempts, locked_until FROM kb_jobs")
    assert [(job.kind, job.attempts, job.locked_until) for job in jobs] == [("index", 0, None)]
    assert await portal.audit_events() == [
        *(await portal.audit_events())[:-1],
        ("document_uploaded", {"document_id": body["id"], "scope": "shared"}),
    ]


async def test_type_is_detected_by_content_not_by_name(portal: Portal) -> None:
    client = await portal.employee()
    disguised = await add_document(client, "картинка.png", TEXT_PDF)
    assert disguised.status_code == 201
    assert (await portal.rows("SELECT media_type FROM kb_documents"))[0].media_type == (
        "application/pdf"
    )
    for name, content in [("page.html", b"<html></html>"), ("архив.docx", b"PK\x03\x04junk")]:
        refused = await add_document(client, name, content)
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            415,
            "unsupported_file_type",
        )
    assert len(await portal.rows("SELECT 1 FROM kb_documents")) == 1
    assert len(list((portal.container.settings.files.root / "kb").iterdir())) == 1


async def test_file_name_is_only_a_title(portal: Portal) -> None:
    client = await portal.employee()
    response = await add_document(client, "..\\..\\etc/../passwd.txt", b"text")
    assert response.json()["title"] == "passwd.txt"
    key = (await portal.rows("SELECT storage_key FROM kb_documents"))[0].storage_key
    assert "passwd" not in key
    assert display_title("C:\\docs\\до\x00го\r\nвор.pdf") == "договор.pdf"
    assert len(display_title("я" * 300 + ".pdf")) == 255
    assert display_title("  ") == "файл"


async def test_unreadable_and_too_long_files_are_refused_and_not_kept(
    settings: Settings, tmp_path: Path
) -> None:
    portal = await make_portal(kb_settings(settings, tmp_path, document_max_pages=2))
    try:
        client = await portal.employee()
        broken = await add_document(client, "битый.pdf", b"%PDF-1.4 not a document")
        assert (broken.status_code, broken.json()["error"]["code"]) == (422, "file_unreadable")
        long = await add_document(client, "длинный.pdf", samples.scan_pdf(3))
        assert (long.status_code, long.json()["error"]["code"]) == (422, "too_many_pages")
        assert long.json()["error"]["details"] == {"max_pages": 2}
        assert await portal.rows("SELECT 1 FROM kb_documents") == []
        assert await portal.rows("SELECT 1 FROM kb_jobs") == []
        assert list((settings.files.root / "kb").iterdir()) == []
    finally:
        await close_portal(portal)


async def test_file_over_limit_is_refused_with_file_too_large(
    settings: Settings, tmp_path: Path
) -> None:
    portal = await make_portal(kb_settings(settings, tmp_path, document_max_bytes=2000))
    try:
        client = await portal.employee()
        # Тело в пределах запаса на поля формы, но сам файл больше лимита.
        response = await add_document(client, "большой.txt", b"a" * 3000)
        assert (response.status_code, response.json()["error"]["code"]) == (413, "file_too_large")
        assert response.json()["error"]["details"] == {"max_bytes": 2000}
        # Тело заведомо больше предела маршрута отклоняется по Content-Length.
        huge = await add_document(client, "огромный.txt", b"a" * 200_000)
        assert (huge.status_code, huge.json()["error"]["code"]) == (413, "file_too_large")
        assert await portal.rows("SELECT 1 FROM kb_documents") == []
        assert list((settings.files.root / "kb").iterdir()) == []
    finally:
        await close_portal(portal)


async def test_upload_has_its_own_body_limit_above_json_limit(portal: Portal) -> None:
    client = await portal.employee()
    limit = portal.container.settings.server.json_body_max_bytes
    response = await add_document(client, "большой.txt", b"a" * (limit + 1000))
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("data", "field", "code"),
    [
        ({"is_cogis": "false"}, "scope", "required"),
        ({"scope": "all", "is_cogis": "false"}, "scope", "unknown_value"),
        ({"scope": "shared"}, "is_cogis", "required"),
        ({"scope": "shared", "is_cogis": "да"}, "is_cogis", "unknown_value"),
        ({"scope": "personal", "is_cogis": "true"}, "is_cogis", "invalid_format"),
    ],
)
async def test_upload_validates_form_fields(
    portal: Portal, data: dict[str, str], field: str, code: str
) -> None:
    client = await portal.employee()
    response = await client.post(
        "/api/kb/documents", files={"file": ("a.txt", b"text", "text/plain")}, data=data
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [(item["field"], item["code"]) for item in error["fields"]] == [(field, code)]
    assert await portal.rows("SELECT 1 FROM kb_documents") == []


async def test_upload_without_file_or_session_or_csrf(portal: Portal) -> None:
    client = await portal.employee()
    missing = await client.post("/api/kb/documents", data={"scope": "shared", "is_cogis": "false"})
    assert [item["field"] for item in missing.json()["error"]["fields"]] == ["file"]
    async with portal.client() as anonymous:
        response = await add_document(anonymous, "a.txt", b"text")
        assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthenticated")
        listing = await anonymous.get("/api/kb/documents", params={"scope": "shared"})
        assert listing.status_code == 401
    client.headers.pop("X-Portal-Csrf")
    forged = await add_document(client, "a.txt", b"text")
    assert (forged.status_code, forged.json()["error"]["code"]) == (403, "csrf_check_failed")


async def test_cogis_mark_is_set_on_upload_and_cannot_be_changed(portal: Portal) -> None:
    client = await portal.employee()
    document_id = await added(client, "sdk.txt", b"plugin api", is_cogis=True)
    assert (await _document(client, document_id))["is_cogis"] is True
    for method in ("patch", "put"):
        response = await client.request(
            method, f"/api/kb/documents/{document_id}", json={"is_cogis": False}
        )
        assert response.status_code == 404  # такого маршрута нет


# --- повтор файла ---


async def test_duplicate_in_shared_base_names_author_and_state(portal: Portal) -> None:
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    document_id = await added(ivanov, "договор.pdf", TEXT_PDF)
    response = await add_document(petrov, "тот же файл под другим именем.pdf", TEXT_PDF)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "duplicate_document"
    assert error["details"] == {
        "document": {
            "id": document_id,
            "title": "договор.pdf",
            "author_full_name": "Пользователь ivanov",
            "created_at": "2026-10-05T09:00:00Z",
            "status": "queued",
            "error_code": None,
            "can_delete": False,
        }
    }
    again = await add_document(ivanov, "договор.pdf", TEXT_PDF)
    assert again.json()["error"]["details"]["document"]["can_delete"] is True
    # Отклонённый файл не сохраняется: на диске один оригинал, в очереди одно задание.
    assert len(list((portal.container.settings.files.root / "kb").iterdir())) == 1
    assert len(await portal.rows("SELECT 1 FROM kb_jobs")) == 1


async def test_duplicate_is_checked_only_inside_target_collection(portal: Portal) -> None:
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    await added(ivanov, "личный.txt", b"same bytes", scope="personal")
    # Тот же файл в личной базе другого сотрудника и в общей базе — не повтор.
    await added(petrov, "личный.txt", b"same bytes", scope="personal")
    await added(petrov, "общий.txt", b"same bytes", scope="shared")
    own = await add_document(ivanov, "ещё раз.txt", b"same bytes", scope="personal")
    assert own.status_code == 409
    assert own.json()["error"]["details"]["document"]["title"] == "личный.txt"
    assert len(await portal.rows("SELECT 1 FROM kb_documents")) == 3


async def test_duplicate_counts_documents_in_error_but_not_deleted_ones(bench: KbBench) -> None:
    client = await bench.portal.employee()
    document_id = await added(client, "пустой.txt", b"   ")
    await bench.drain()
    assert (await _document(client, document_id))["status"] == "error"
    refused = await add_document(client, "пустой.txt", b"   ")
    assert refused.status_code == 409
    existing = refused.json()["error"]["details"]["document"]
    assert (existing["status"], existing["error_code"]) == ("error", "no_text")
    assert (await client.delete(f"/api/kb/documents/{document_id}")).status_code == 204
    # Удалённый документ повтором не считается, даже пока воркер его не стёр.
    assert (await add_document(client, "пустой.txt", b"   ")).status_code == 201


async def test_concurrent_identical_uploads_create_one_document(portal: Portal) -> None:
    client = await portal.employee()
    responses = await asyncio.gather(
        *(add_document(client, f"копия {n}.txt", b"one content") for n in range(4))
    )
    assert sorted(response.status_code for response in responses) == [201, 409, 409, 409]
    assert len(await portal.rows("SELECT 1 FROM kb_documents")) == 1
    assert len(list((portal.container.settings.files.root / "kb").iterdir())) == 1


# --- список ---


async def test_list_filters_by_collection_and_owner(portal: Portal) -> None:
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    await added(ivanov, "общий иванова.txt", b"1")
    await added(petrov, "общий петрова.txt", b"2")
    await added(ivanov, "личный иванова.txt", b"3", scope="personal")
    await added(petrov, "личный петрова.txt", b"4", scope="personal")

    shared = (await ivanov.get("/api/kb/documents", params={"scope": "shared"})).json()
    assert {item["title"] for item in shared["items"]} == {"общий иванова.txt", "общий петрова.txt"}
    assert (shared["page"], shared["page_size"], shared["total"]) == (1, 50, 2)
    personal = (await ivanov.get("/api/kb/documents", params={"scope": "personal"})).json()
    assert [item["title"] for item in personal["items"]] == ["личный иванова.txt"]
    assert personal["total"] == 1


async def test_list_searches_title_and_author_sorts_and_pages(portal: Portal) -> None:
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    for index, (client, name) in enumerate(
        [(ivanov, "Акт.txt"), (petrov, "договор 100%.txt"), (ivanov, "Выписка_1.txt")]
    ):
        portal.clock.advance(minutes=1)
        await added(client, name, f"content {index}".encode())

    async def titles(**params: Any) -> list[str]:
        response = await ivanov.get("/api/kb/documents", params={"scope": "shared", **params})
        assert response.status_code == 200, response.text
        return [item["title"] for item in response.json()["items"]]

    assert await titles() == ["Выписка_1.txt", "договор 100%.txt", "Акт.txt"]  # новые сверху
    assert await titles(order="asc") == ["Акт.txt", "договор 100%.txt", "Выписка_1.txt"]
    assert await titles(sort="title") == ["Акт.txt", "Выписка_1.txt", "договор 100%.txt"]
    assert await titles(sort="title", order="desc") == [
        "договор 100%.txt",
        "Выписка_1.txt",
        "Акт.txt",
    ]
    assert await titles(q="ВЫПИСКА") == ["Выписка_1.txt"]
    assert await titles(q="petrov") == ["договор 100%.txt"]  # по ФИО автора
    assert await titles(q="100%") == ["договор 100%.txt"]  # знаки шаблона — обычные символы
    assert await titles(q="_") == ["Выписка_1.txt"]
    assert await titles(page=2, page_size=2) == ["Акт.txt"]
    bad_sort = await ivanov.get("/api/kb/documents", params={"scope": "shared", "sort": "size"})
    assert bad_sort.json()["error"]["fields"][0] == {
        "field": "sort",
        "code": "unknown_value",
        "message": "Недопустимое значение.",
    }
    no_scope = await ivanov.get("/api/kb/documents")
    assert no_scope.json()["error"]["fields"][0]["field"] == "scope"


# --- изоляция: чужой личный документ → 404 на каждом маршруте ---


@pytest.mark.parametrize("stranger_role", ["employee", "admin"])
async def test_foreign_personal_document_is_not_found_on_every_route(
    bench: KbBench, stranger_role: str
) -> None:
    portal = bench.portal
    owner = await portal.employee("ivanov")
    stranger = portal.client()
    await portal.onboard(stranger, "stranger", stranger_role)
    document_id = await added(
        owner, "личный договор.txt", "Тайный срок аренды".encode(), "personal"
    )
    await bench.drain()
    assert (await _document(owner, document_id))["status"] == "ready"
    fragment_id = (await portal.rows("SELECT id FROM kb_fragments"))[0].id

    base = f"/api/kb/documents/{document_id}"
    attempts = [
        await stranger.get(base),
        await stranger.get(f"{base}/text"),
        await stranger.get(f"{base}/text", params={"page": 1, "fragment_id": str(fragment_id)}),
        await stranger.get(f"{base}/file"),
        await stranger.post(f"{base}/retry"),
        await stranger.delete(base),
    ]
    for response in attempts:
        assert response.status_code == 404, response.request.url
        assert response.json()["error"]["code"] == "not_found"
        assert "Тайный" not in response.text and "личный договор" not in response.text

    listing = await stranger.get("/api/kb/documents", params={"scope": "personal"})
    assert listing.json() == {"items": [], "page": 1, "page_size": 50, "total": 0}
    shared = await stranger.get("/api/kb/documents", params={"scope": "shared", "q": "договор"})
    assert shared.json()["total"] == 0
    # Тот же файл у постороннего повтором не считается: по отказу нельзя узнать о чужом.
    same = await add_document(stranger, "копия.txt", "Тайный срок аренды".encode(), "personal")
    assert same.status_code == 201
    shared_copy = await add_document(stranger, "копия.txt", "Тайный срок аренды".encode())
    assert shared_copy.status_code == 201

    # Документ владельца не тронут ни одной из попыток.
    assert (await _document(owner, document_id))["status"] == "ready"
    events = [event for event, _ in await portal.audit_events()]
    assert "document_deleted" not in events and "document_retried" not in events


async def test_unknown_and_malformed_document_ids_are_not_found(portal: Portal) -> None:
    client = await portal.employee()
    for document_id in ("00000000-0000-4000-8000-000000000000", "not-a-uuid"):
        for path in ("", "/text", "/file"):
            response = await client.get(f"/api/kb/documents/{document_id}{path}")
            assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")


# --- удаление и повторная обработка ---


async def test_delete_rights_author_admin_and_other_employee(bench: KbBench) -> None:
    portal = bench.portal
    author = await portal.employee("ivanov")
    other = await portal.employee("petrov")
    admin = portal.client()
    await portal.onboard(admin, "boss", "admin")
    first = await added(author, "первый.txt", b"first")
    second = await added(author, "второй.txt", b"second")

    assert (await _document(other, first))["can_delete"] is False
    assert (await _document(admin, first))["can_delete"] is True
    refused = await other.delete(f"/api/kb/documents/{first}")
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "forbidden")

    assert (await author.delete(f"/api/kb/documents/{first}")).status_code == 204
    assert (await admin.delete(f"/api/kb/documents/{second}")).status_code == 204
    # С этого момента документа нет ни в одном маршруте, хотя воркер его ещё не стёр.
    for client in (author, admin):
        assert (await client.get(f"/api/kb/documents/{first}")).status_code == 404
        assert (await client.get(f"/api/kb/documents/{first}/file")).status_code == 404
        assert (await client.delete(f"/api/kb/documents/{first}")).status_code == 404
    assert (await author.get("/api/kb/documents", params={"scope": "shared"})).json()["total"] == 0
    jobs = await portal.rows("SELECT kind FROM kb_jobs ORDER BY created_at")
    assert [job.kind for job in jobs] == ["delete", "delete"]  # задания индексации сняты
    deletions = [d for event, d in await portal.audit_events() if event == "document_deleted"]
    assert deletions == [
        {"document_id": first, "scope": "shared", "by_admin": False},
        {"document_id": second, "scope": "shared", "by_admin": True},
    ]


async def test_retry_returns_failed_document_to_queue(bench: KbBench) -> None:
    portal = bench.portal
    author = await portal.employee("ivanov")
    other = await portal.employee("petrov")
    admin = portal.client()
    await portal.onboard(admin, "boss", "admin")
    document_id = await added(author, "скан.png", samples.png())
    path = f"/api/kb/documents/{document_id}/retry"

    not_failed = await author.post(path)
    assert (not_failed.status_code, not_failed.json()["error"]["code"]) == (
        409,
        "document_not_in_error",
    )
    await portal.execute(
        "UPDATE kb_documents SET status = 'error', error_code = 'recognition_failed'"
    )
    await portal.execute("DELETE FROM kb_jobs")
    assert (await _document(author, document_id))["error_code"] == "recognition_failed"

    refused = await other.post(path)
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "forbidden")
    response = await admin.post(path)
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["error_code"]) == ("queued", None)
    jobs = await portal.rows("SELECT kind, attempts FROM kb_jobs")
    assert [(job.kind, job.attempts) for job in jobs] == [("index", 0)]
    again = await author.post(path)
    assert again.json()["error"]["code"] == "document_not_in_error"
    assert len(await portal.rows("SELECT 1 FROM kb_jobs")) == 1
    retried = [d for event, d in await portal.audit_events() if event == "document_retried"]
    assert retried == [{"document_id": document_id, "scope": "shared", "by_admin": True}]


async def test_audit_has_no_titles_or_content(bench: KbBench) -> None:
    client = await bench.portal.employee()
    document_id = await added(client, "Секретное название.txt", "Секретное содержимое".encode())
    await bench.drain()
    await client.delete(f"/api/kb/documents/{document_id}")
    rows = await bench.portal.rows("SELECT event, details::text AS details, ip FROM audit_log")
    kb_rows = [row for row in rows if row.event.startswith("document_")]
    assert [row.event for row in kb_rows] == ["document_uploaded", "document_deleted"]
    assert all("Секрет" not in row.details and row.ip is not None for row in kb_rows)


# --- состояние, текст страницы, оригинал ---


async def test_progress_is_shown_only_while_processing_paged_document(portal: Portal) -> None:
    client = await portal.employee()
    document_id = await added(client, "скан.pdf", samples.scan_pdf(3))
    await portal.execute(
        "UPDATE kb_documents SET status = 'processing', pages_done = 1, recognizing = true"
    )
    assert (await _document(client, document_id))["progress"] == {
        "pages_done": 1,
        "pages_total": 3,
        "recognizing": True,
    }
    await portal.execute("UPDATE kb_documents SET status = 'ready'")
    assert (await _document(client, document_id))["progress"] is None
    text_id = await added(client, "заметка.txt", b"note")
    await portal.execute("UPDATE kb_documents SET status = 'processing'")
    assert (await _document(client, text_id))["progress"] is None  # страниц нет


async def test_page_text_with_highlighted_quote(bench: KbBench) -> None:
    portal = bench.portal
    author = await portal.employee("ivanov")
    reader = await portal.employee("petrov")
    first = "First page of the lease agreement about the land plot"
    second = "Second page states the term of forty nine years exactly"
    document_id = await added(author, "договор.pdf", samples.text_pdf([first, second]))
    path = f"/api/kb/documents/{document_id}/text"

    early = await reader.get(path)
    assert (early.status_code, early.json()["error"]["code"]) == (409, "document_not_ready")
    await bench.drain()
    fragments = await portal.rows("SELECT id, page_number FROM kb_fragments ORDER BY ordinal")
    assert [fragment.page_number for fragment in fragments] == [1, 2]

    default = (await reader.get(path)).json()
    assert default == {
        "page": 1,
        "page_count": 2,
        "recognized": False,
        "segments": [{"text": first, "highlight": False}],
    }
    by_fragment = (await reader.get(path, params={"fragment_id": str(fragments[1].id)})).json()
    assert (by_fragment["page"], by_fragment["segments"]) == (
        2,
        [{"text": second, "highlight": True}],
    )
    # Задана страница — отдаётся она; цитата с другой страницы не подсвечивается.
    other_page = (
        await reader.get(path, params={"page": 1, "fragment_id": str(fragments[1].id)})
    ).json()
    assert other_page["segments"] == [{"text": first, "highlight": False}]
    # Неизвестный фрагмент — текст без подсветки, это не ошибка.
    unknown = await reader.get(path, params={"fragment_id": "00000000-0000-4000-8000-000000000000"})
    assert unknown.json()["segments"] == [{"text": first, "highlight": False}]
    assert (await reader.get(path, params={"page": 3})).status_code == 404
    assert (await reader.get(path, params={"page": 0})).status_code == 404
    malformed = await reader.get(path, params={"fragment_id": "x"})
    assert malformed.json()["error"]["code"] == "validation_error"


async def test_quote_is_a_middle_segment_and_segments_rebuild_the_page(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    text = "Начало страницы. Цитата из договора. Конец страницы."
    document_id = await added(client, "без страниц.txt", text.encode())
    await bench.drain()
    start = text.index("Цитата")
    await portal.execute(
        "UPDATE kb_fragments SET start_offset = :s, end_offset = :e",
        s=start,
        e=start + len("Цитата из договора."),
    )
    fragment_id = (await portal.rows("SELECT id FROM kb_fragments"))[0].id
    body = (
        await client.get(
            f"/api/kb/documents/{document_id}/text",
            params={"fragment_id": str(fragment_id), "page": 7},
        )
    ).json()
    # У документа без страниц `page` игнорируется, а номера страницы нет.
    assert (body["page"], body["page_count"], body["recognized"]) == (None, None, False)
    assert body["segments"] == [
        {"text": "Начало страницы. ", "highlight": False},
        {"text": "Цитата из договора.", "highlight": True},
        {"text": " Конец страницы.", "highlight": False},
    ]
    assert "".join(segment["text"] for segment in body["segments"]) == text


async def test_fragment_of_another_document_is_not_highlighted(bench: KbBench) -> None:
    client = await bench.portal.employee()
    first = await added(client, "первый.txt", "Текст первого документа".encode())
    await added(client, "второй.txt", "Текст второго документа".encode())
    await bench.drain()
    foreign = (
        await bench.portal.rows("SELECT id FROM kb_fragments WHERE document_id <> :id", id=first)
    )[0].id
    body = (
        await client.get(f"/api/kb/documents/{first}/text", params={"fragment_id": str(foreign)})
    ).json()
    assert body["segments"] == [{"text": "Текст первого документа", "highlight": False}]


@pytest.mark.parametrize(
    ("name", "content", "content_type", "disposition"),
    [
        ("договор.pdf", TEXT_PDF, "application/pdf", "inline"),
        ("фото.png", samples.png(), "image/png", "inline"),
        ("заметка.md", b"# note", "text/plain; charset=utf-8", "inline"),
        ("регламент.docx", samples.docx_file("text"), DOCX_TYPE, "attachment"),
        ("page.txt", b"<script>alert(1)</script>", "text/plain; charset=utf-8", "inline"),
    ],
)
async def test_original_file_is_served_in_any_state_with_safe_headers(
    portal: Portal, name: str, content: bytes, content_type: str, disposition: str
) -> None:
    author = await portal.employee("ivanov")
    reader = await portal.employee("petrov")
    document_id = await added(author, name, content)
    response = await reader.get(f"/api/kb/documents/{document_id}/file")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == content_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith(f"{disposition}; filename*=UTF-8''")
    assert response.headers["cache-control"] == "no-store"


async def test_cogis_documentation_is_available_only_when_ready(bench: KbBench) -> None:
    client = await bench.portal.employee()
    path = "/api/kb/cogis-documentation"
    await added(client, "обычный.txt", "обычный документ".encode())
    await bench.drain()
    assert (await client.get(path)).json() == {"available": False}
    document_id = await added(client, "sdk.txt", "документация плагинов".encode(), is_cogis=True)
    assert (await client.get(path)).json() == {"available": False}  # ещё в очереди
    await bench.drain()
    assert (await client.get(path)).json() == {"available": True}
    assert await bench.knowledge.has_cogis_documentation() is True
    await client.delete(f"/api/kb/documents/{document_id}")
    assert (await client.get(path)).json() == {"available": False}
