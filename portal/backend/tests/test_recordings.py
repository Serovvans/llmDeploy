"""Записи во вложениях чата: приём телом запроса, состояние, расшифровка (§1.6, §5.8)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from portal.core.settings import Settings
from portal.dialogs.transcripts import SqlAttachmentTranscripts
from portal.files.ports import StoredRecording
from portal.llm.ports import TextPart
from tests.conftest import close_portal, make_portal
from tests.support import CHAT_BODY, Portal, ask, new_dialog, new_tool_dialog, upload

pytestmark = pytest.mark.anyio

WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 100

type MakePortal = Callable[..., Awaitable[Portal]]


@pytest.fixture
async def media_portal(settings: Settings) -> AsyncIterator[MakePortal]:
    """Портал с пределами приёма записей, которые задаёт тест; свободное место не проверяется."""
    built: list[Portal] = []

    async def make(**overrides: float) -> Portal:
        media = settings.media.model_copy(update={"min_free_bytes": 0, **overrides})
        built.append(await make_portal(settings.model_copy(update={"media": media})))
        return built[-1]

    yield make
    for portal in built:
        await close_portal(portal)


def _media_files(portal: Portal) -> list[Path]:
    root = portal.container.settings.files.root / "media"
    return [path for path in root.rglob("*") if path.is_file()]


async def _held(first: bytes, release: asyncio.Event, rest: bytes = b"") -> AsyncIterator[bytes]:
    """Тело, которое после первой части ждёт разрешения теста."""
    yield first
    await release.wait()
    if rest:
        yield rest


async def send_recording(
    client: httpx.AsyncClient,
    dialog_id: str,
    content: bytes | AsyncIterator[bytes] = WAV,
    *,
    declared: int | None = None,
    name: str | None = "совещание.wav",
) -> httpx.Response:
    """Загрузить запись телом запроса; `declared` — заявленная длина потокового тела."""
    headers = {} if declared is None else {"Content-Length": str(declared)}
    return await client.post(
        f"/api/dialogs/{dialog_id}/attachments/media",
        params={} if name is None else {"file_name": name},
        content=content,
        headers=headers,
    )


async def _recording(portal: Portal, client: httpx.AsyncClient, dialog_id: str) -> str:
    response = await send_recording(client, dialog_id)
    assert response.status_code == 201, response.text
    attachment_id: str = response.json()["id"]
    return attachment_id


async def test_recording_is_queued_shown_only_to_its_owner_and_never_served(
    media_portal: MakePortal,
) -> None:
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    response = await send_recording(client, dialog_id, name="C:\\записи\\совещание.wav")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "id": body["id"],
        "file_name": "совещание.wav",
        "media_type": "audio/wav",
        "page_count": None,
        "image_count": 0,
        "duration_seconds": None,
        "transcription": {"status": "queued", "error_code": None},
        "created_at": "2026-10-05T09:00:00Z",
    }
    owner = await portal.user_id("ivanov")
    (stored,) = _media_files(portal)
    assert stored.parent.name == str(owner) and stored.read_bytes() == WAV
    assert portal.transcriptions.enqueued == [UUID(body["id"])]

    path = f"/api/dialogs/{dialog_id}/attachments/{body['id']}"
    assert (await client.get(path)).json() == body
    # Исходный файл записи не отдаётся никому, в том числе владельцу (§1.5).
    assert (await client.get(f"{path}/file")).status_code == 404

    # Чужая запись и её состояние не существуют — и для администратора тоже (§5.8).
    admin = portal.client()
    await portal.onboard(admin, "boss", "admin")
    assert (await admin.get(path)).status_code == 404
    assert (await send_recording(admin, dialog_id)).status_code == 404
    assert len(_media_files(portal)) == 1


async def test_refusals_come_before_the_body_is_read(media_portal: MakePortal) -> None:
    portal = await media_portal(max_bytes=1000)
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    sql_dialog = await new_tool_dialog(client, "sql")
    read: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        read.append(WAV)
        yield WAV

    async def refusal(
        sender: httpx.AsyncClient, target: str, declared: int | None = len(WAV), **extra: Any
    ) -> tuple[int, str]:
        response = await send_recording(sender, target, body(), declared=declared, **extra)
        return response.status_code, response.json()["error"]["code"]

    anonymous = portal.client()
    stranger = portal.client()
    await portal.onboard(stranger, "petrov")
    # Порядок §1.6: длина тела проверяется до сессии, сессия — до цели и параметров.
    assert await refusal(anonymous, dialog_id, None) == (411, "length_required")
    assert await refusal(anonymous, dialog_id, 1001) == (413, "file_too_large")
    assert await refusal(anonymous, dialog_id) == (401, "unauthenticated")
    assert await refusal(stranger, dialog_id, name=None) == (404, "not_found")
    assert await refusal(client, str(uuid4())) == (404, "not_found")
    assert await refusal(client, sql_dialog) == (409, "wrong_dialog_kind")
    assert await refusal(client, dialog_id, name=None) == (422, "validation_error")
    assert read == [] and _media_files(portal) == []

    too_large = await send_recording(client, dialog_id, body(), declared=1001)
    assert too_large.json()["error"]["details"] == {"max_bytes": 1000}
    # Тот же предел тела и то же требование длины — у маршрута записи базы знаний (§1.3).
    kb = await client.post("/api/kb/documents/media", content=body())
    assert (kb.status_code, kb.json()["error"]["code"]) == (411, "length_required")
    kb = await client.post(
        "/api/kb/documents/media", content=body(), headers={"Content-Length": "1001"}
    )
    assert (kb.status_code, kb.json()["error"]["code"]) == (413, "file_too_large")
    # Обычный маршрут вложений записи не принимает (§1.5).
    assert (await upload(client, dialog_id, "совещание.wav", WAV)).status_code == 415
    assert read == []


async def test_disabled_feature_refuses_the_upload(media_portal: MakePortal) -> None:
    portal = await media_portal(enabled=False)
    client = await portal.employee()
    response = await send_recording(client, await new_dialog(client))
    assert (response.status_code, response.json()["error"]["code"]) == (403, "feature_disabled")
    assert _media_files(portal) == []


async def test_reception_refusals_leave_no_file_and_no_row(media_portal: MakePortal) -> None:
    portal = await media_portal(quota_bytes=300, user_quota_share=0.5)
    client = await portal.employee()
    dialog_id = await new_dialog(client)

    not_recording = await send_recording(client, dialog_id, b"%PDF-1.7" + b"\x00" * 100)
    assert not_recording.status_code == 415
    assert not_recording.json()["error"]["code"] == "unsupported_file_type"

    own_quota = await send_recording(client, dialog_id, WAV + b"\x00" * 40)
    assert own_quota.status_code == 507
    assert own_quota.json()["error"]["code"] == "media_quota_exceeded"
    assert own_quota.json()["error"]["details"] == {"reason": "user"}
    assert "удалив свои записи" in own_quota.json()["error"]["message"]
    total_quota = await send_recording(client, dialog_id, WAV + b"\x00" * 200)
    assert total_quota.json()["error"]["details"] == {"reason": "total"}
    assert "администратору" in total_quota.json()["error"]["message"]

    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []
    assert portal.transcriptions.enqueued == []


async def test_second_upload_of_the_same_employee_is_refused_until_the_first_ends(
    media_portal: MakePortal,
) -> None:
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    release = asyncio.Event()
    first = asyncio.create_task(
        send_recording(client, dialog_id, _held(WAV, release, b"tail"), declared=len(WAV) + 4)
    )
    while not _media_files(portal):
        await asyncio.sleep(0.01)
    second = await send_recording(client, dialog_id)
    assert (second.status_code, second.json()["error"]["code"]) == (429, "too_many_uploads")
    release.set()
    assert (await first).status_code == 201
    assert (await send_recording(client, dialog_id)).status_code == 201


@pytest.mark.parametrize(
    ("limits", "text"),
    [
        ({"upload_idle_timeout_seconds": 0.1}, "данные перестали поступать"),
        ({"upload_timeout_seconds": 0.2}, "слишком долго"),
    ],
)
async def test_stalled_and_endless_uploads_answer_upload_timeout(
    media_portal: MakePortal, limits: dict[str, float], text: str
) -> None:
    portal = await media_portal(**limits)
    client = await portal.employee()
    dialog_id = await new_dialog(client)

    async def trickle() -> AsyncIterator[bytes]:
        yield WAV
        while "upload_timeout_seconds" in limits:
            await asyncio.sleep(0.02)
            yield b"\x00"
        await asyncio.Event().wait()

    response = await send_recording(client, dialog_id, trickle(), declared=10**6)
    assert (response.status_code, response.json()["error"]["code"]) == (408, "upload_timeout")
    assert text in response.json()["error"]["message"]
    assert _media_files(portal) == []
    # Место загрузки освобождено: следующая загрузка того же сотрудника принимается.
    assert (await send_recording(client, dialog_id)).status_code == 201


async def test_session_deleted_during_reception_cuts_the_upload(media_portal: MakePortal) -> None:
    """Блокировка во время приёма освобождает место загрузки и резерв сразу (§1.6)."""
    portal = await media_portal(max_concurrent_uploads=1)
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    upload_task = asyncio.create_task(
        send_recording(client, dialog_id, _held(WAV, asyncio.Event()), declared=10**6)
    )
    while not _media_files(portal):
        await asyncio.sleep(0.01)
    actor = await portal.actor()
    await portal.container.admin.set_blocked(actor, await portal.user_id("ivanov"), blocked=True)

    response = await asyncio.wait_for(upload_task, timeout=5)
    assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthenticated")
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []
    # Единственное место загрузки свободно: другой сотрудник загружает запись.
    other = portal.client()
    await portal.onboard(other, "petrov")
    assert (await send_recording(other, await new_dialog(other))).status_code == 201


@pytest.mark.parametrize(
    ("statement", "status"),
    [("DELETE FROM sessions", 401), ("DELETE FROM dialogs", 404)],
)
async def test_session_and_dialog_are_checked_again_after_reception(
    media_portal: MakePortal, monkeypatch: pytest.MonkeyPatch, statement: str, status: int
) -> None:
    """Право могло исчезнуть, пока шёл приём: строка не создаётся, файл удаляется (§1.6)."""
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    storage = portal.container.media_storage
    original = storage.receive

    async def receive_then_lose(*args: Any) -> StoredRecording:
        recording = await original(*args)
        await portal.execute(statement)
        return recording

    monkeypatch.setattr(storage, "receive", receive_then_lose)
    assert (await send_recording(client, dialog_id)).status_code == status
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []
    assert portal.transcriptions.enqueued == []


async def test_recording_without_a_job_is_not_left_waiting(media_portal: MakePortal) -> None:
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    portal.transcriptions.failure = ConnectionError("очередь недоступна")
    response = await send_recording(client, dialog_id)
    assert (response.status_code, response.json()["error"]["code"]) == (503, "service_unavailable")
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []


async def test_startup_fails_queued_recordings_that_have_no_job(media_portal: MakePortal) -> None:
    """Остановка между фиксацией строки и постановкой задания (§5.8)."""
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    kept = await _recording(portal, client, dialog_id)
    lost = await _recording(portal, client, dialog_id)
    portal.transcriptions.enqueued.remove(UUID(lost))
    kept_key = (await portal.rows("SELECT storage_key FROM attachments WHERE id = :i", i=kept))[0]

    await portal.container.dialogs.align_recordings()

    rows = await portal.rows(
        "SELECT id, transcription_status, transcription_error FROM attachments ORDER BY created_at"
    )
    assert {str(row.id): tuple(row)[1:] for row in rows} == {
        kept: ("queued", None),
        lost: ("error", "internal_error"),
    }
    root = portal.container.settings.files.root
    assert _media_files(portal) == [root / kept_key.storage_key]


async def test_message_waits_for_the_transcript_and_carries_it_as_attachment_text(
    media_portal: MakePortal,
) -> None:
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    attachment_id = await _recording(portal, client, dialog_id)
    transcripts = SqlAttachmentTranscripts(portal.container.engine)
    body = {**CHAT_BODY, "attachment_ids": [attachment_id]}

    async def refused() -> None:
        response = await client.post(f"/api/dialogs/{dialog_id}/messages", json=body)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "attachment_not_ready"
        assert response.json()["error"]["details"] == {"attachment_id": attachment_id}

    await refused()
    source = await transcripts.begin(UUID(attachment_id))
    assert source is not None and source.media_type == "audio/wav"
    assert portal.container.media_storage.path(source.storage_key).read_bytes() == WAV
    await refused()
    assert await portal.rows("SELECT 1 FROM messages") == []

    await transcripts.complete(UUID(attachment_id), "Обсудили срок аренды: 49 лет.", 75)
    await portal.container.media_storage.delete(source.storage_key)  # как воркер после задания
    shown = (await client.get(f"/api/dialogs/{dialog_id}/attachments/{attachment_id}")).json()
    assert shown["transcription"] == {"status": "ready", "error_code": None}
    assert shown["duration_seconds"] == 75

    events = await ask(client, dialog_id, attachment_ids=[attachment_id])
    assert events[-1] == ("done", {"status": "complete"})
    (part,) = portal.model.requests[0].messages[-1].parts
    assert isinstance(part, TextPart)
    assert part.text == (
        '<вложение имя="совещание.wav">\nОбсудили срок аренды: 49 лет.\n</вложение>\n\n'
        "Какой срок аренды?"
    )
    # Удаление чата с записью, исходника которой уже нет, проходит.
    assert (await client.delete(f"/api/dialogs/{dialog_id}")).status_code == 204


async def test_failed_recording_is_never_sent(media_portal: MakePortal) -> None:
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    attachment_id = await _recording(portal, client, dialog_id)
    transcripts = SqlAttachmentTranscripts(portal.container.engine)
    await transcripts.begin(UUID(attachment_id))
    await transcripts.fail(UUID(attachment_id), "media_too_long", 4000)
    # Опоздавший итог расшифровки ошибку не затирает.
    await transcripts.complete(UUID(attachment_id), "текст", 4000)

    path = f"/api/dialogs/{dialog_id}/attachments/{attachment_id}"
    shown = (await client.get(path)).json()
    assert shown["transcription"] == {"status": "error", "error_code": "media_too_long"}
    assert shown["duration_seconds"] == 4000
    response = await client.post(
        f"/api/dialogs/{dialog_id}/messages", json={**CHAT_BODY, "attachment_ids": [attachment_id]}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "attachment_not_ready")
    assert await transcripts.begin(UUID(attachment_id)) is None


async def test_transcripts_port_follows_the_worker(media_portal: MakePortal) -> None:
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    recording_id = UUID(await _recording(portal, client, dialog_id))
    await upload(client, dialog_id, "заметка.txt", b"text")
    transcripts = SqlAttachmentTranscripts(portal.container.engine)

    async def status() -> list[str]:
        rows = await portal.rows(
            "SELECT transcription_status FROM attachments WHERE id = :id", id=recording_id
        )
        return [row.transcription_status for row in rows]

    (key,) = await transcripts.source_keys()  # документ-вложение записью не считается
    assert key.startswith("media/")
    assert await transcripts.begin(recording_id) is not None
    await transcripts.requeue(recording_id)
    assert await status() == ["queued"]
    # Воркер погиб, не вернув вложение в очередь: задание берётся снова.
    assert await transcripts.begin(recording_id) is not None
    assert await transcripts.begin(recording_id) is not None
    assert await status() == ["processing"]

    # Вложение удалено, пока шла расшифровка: файл убран, итог записать некуда.
    path = f"/api/dialogs/{dialog_id}/attachments/{recording_id}"
    assert (await client.delete(path)).status_code == 204
    assert _media_files(portal) == []
    assert await transcripts.begin(recording_id) is None
    await transcripts.complete(recording_id, "текст", 10)
    await transcripts.fail(recording_id, "internal_error", None)
    assert await status() == [] and await transcripts.source_keys() == set()


async def test_unsent_recordings_are_evicted_and_dropped_with_their_files(
    settings: Settings, media_portal: MakePortal
) -> None:
    """Правила неотправленных вложений действуют и для записей (§5.8)."""
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    first = await _recording(portal, client, dialog_id)
    portal.clock.advance(seconds=1)  # порядок вложений — по времени загрузки
    for index in range(settings.chat.max_attachments - 1):
        assert (await upload(client, dialog_id, f"{index}.txt", b"text")).status_code == 201
    portal.clock.advance(seconds=1)
    # Загрузка сверх предела вытесняет самое старое неотправленное — запись.
    last = (await upload(client, dialog_id, "последний.txt", b"text")).json()["id"]
    assert await portal.rows("SELECT 1 FROM attachments WHERE id = :id", id=first) == []
    assert _media_files(portal) == []

    # Запись, не вошедшая в вопрос, — остаток: строка и файл удаляются.
    await _recording(portal, client, dialog_id)
    await ask(client, dialog_id, attachment_ids=[last])
    for _ in range(100):
        if not _media_files(portal):
            break
        await asyncio.sleep(0.01)
    assert _media_files(portal) == []
    assert len(await portal.rows("SELECT 1 FROM attachments")) == 1


async def test_deleting_a_recording_or_its_dialog_removes_the_source_file(
    media_portal: MakePortal,
) -> None:
    """Исходник записи лежит в `media/`: удаление идёт через его хранилище (§5.8)."""
    portal = await media_portal()
    client = await portal.employee()
    dialog_id = await new_dialog(client)
    first = await _recording(portal, client, dialog_id)
    assert len(_media_files(portal)) == 1
    deleted = await client.delete(f"/api/dialogs/{dialog_id}/attachments/{first}")
    assert deleted.status_code == 204, deleted.text
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []

    await _recording(portal, client, dialog_id)
    assert len(_media_files(portal)) == 1
    deleted = await client.delete(f"/api/dialogs/{dialog_id}")
    assert deleted.status_code == 204, deleted.text
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []

    # Уборка пустых диалогов убирает и запись, оставшуюся в таком диалоге.
    empty = await new_dialog(client)
    await _recording(portal, client, empty)
    portal.clock.advance(days=30)
    await portal.container.dialogs.purge_empty()
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM attachments") == []
