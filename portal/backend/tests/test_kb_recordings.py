"""Записи в базе знаний: приём, расшифровка частями, звук, исходники и уборка (§8.1, §8.7).

ffmpeg и модель речи подменены портами (`FakeAudioTools`, `ScriptedSpeech`); настоящий
запуск ffmpeg проверяет `test_media_audio.py`.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from portal.core.errors import AppError
from portal.core.ports import CurrentUser
from portal.core.settings import Settings
from tests.conftest import close_portal, make_kb_bench
from tests.kb_support import SHARED, KbBench, kb_settings
from tests.support import Portal, new_dialog

pytestmark = pytest.mark.anyio

WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(100)
OPUS = b"OggS" + bytes(range(96))
PATH = "/api/kb/documents/media"

type MakeBench = Callable[..., Awaitable[KbBench]]


@pytest.fixture
async def recording_bench(settings: Settings, tmp_path: Path) -> AsyncIterator[MakeBench]:
    """Стенд с параметрами записей и воркера, которые задаёт тест.

    Части расшифровки — по 30 секунд: запись стенда в 70 секунд даёт три части. Свободное
    место тома не проверяется.
    """
    built: list[KbBench] = []

    async def make(worker: dict[str, Any] | None = None, **media: Any) -> KbBench:
        tuned = settings.media.model_copy(update={"min_free_bytes": 0, "part_seconds": 30, **media})
        prepared = kb_settings(settings, tmp_path, worker=worker or {})
        built.append(await make_kb_bench(prepared.model_copy(update={"media": tuned})))
        return built[-1]

    yield make
    for bench in built:
        await close_portal(bench.portal)


async def send(
    client: httpx.AsyncClient,
    content: bytes | AsyncIterator[bytes] = WAV,
    *,
    scope: str | None = "personal",
    name: str | None = "совещание.wav",
    tags: Sequence[str] = (),
    declared: int | None = None,
) -> httpx.Response:
    """Загрузить запись телом запроса; `declared` — заявленная длина потокового тела."""
    params: dict[str, Any] = {"tags": list(tags)}
    if scope is not None:
        params["scope"] = scope
    if name is not None:
        params["file_name"] = name
    headers = {} if declared is None else {"Content-Length": str(declared)}
    return await client.post(PATH, params=params, content=content, headers=headers)


async def sent(client: httpx.AsyncClient, content: bytes = WAV, **params: Any) -> str:
    response = await send(client, content, **params)
    assert response.status_code == 201, response.text
    document_id: str = response.json()["id"]
    return document_id


def _media_files(portal: Portal) -> list[Path]:
    root = portal.container.settings.files.root / "media"
    return [path for path in root.rglob("*") if path.is_file()]


async def _document(client: httpx.AsyncClient, document_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"/api/kb/documents/{document_id}")).json()
    return body


async def _row(portal: Portal, document_id: str) -> Any:
    (row,) = await portal.rows(
        "SELECT status, error_code, storage_key, audio_key, duration_seconds, seconds_done"
        " FROM kb_documents WHERE id = :id",
        id=document_id,
    )
    return row


def _code(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def test_recording_is_transcribed_in_parts_and_found_with_a_timecode(
    recording_bench: MakeBench,
) -> None:
    bench = await recording_bench()
    portal = bench.portal
    client = await portal.employee()
    response = await send(
        client, scope="shared", name="C:\\записи\\совещание.wav", tags=["Планёрки"]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    document_id = body["id"]
    assert (body["kind"], body["title"], body["status"]) == ("recording", "совещание.wav", "queued")
    assert (body["page_count"], body["duration_seconds"], body["tags"]) == (
        None,
        None,
        ["Планёрки"],
    )
    # Исходный файл записи не отдаётся никогда; звука до конца обработки нет.
    assert (await client.get(f"/api/kb/documents/{document_id}/file")).status_code == 404
    early = await client.get(f"/api/kb/documents/{document_id}/audio")
    assert _code(early) == (409, "document_not_ready")

    assert await bench.drain() == 1
    document = await _document(client, document_id)
    assert (document["status"], document["duration_seconds"]) == ("ready", 70)
    assert document["progress"] is None and document["page_count"] is None
    parts = await portal.rows(
        "SELECT number, text, recognized, start_ms, marks FROM kb_pages ORDER BY number"
    )
    assert [(part.number, part.text, part.start_ms, part.marks) for part in parts] == [
        (1, "Расшифровка-заглушка, отрезок 1.", 0, [[0, 0]]),
        (2, "Расшифровка-заглушка, отрезок 2.", 30_000, [[0, 30_000]]),
        (3, "Расшифровка-заглушка, отрезок 3.", 60_000, [[0, 60_000]]),
    ]
    assert all(part.recognized for part in parts)
    # Формат, определённый по сигнатуре, передан проверке явно; вход — один файл в каталоге.
    assert [media_type for _, media_type in bench.audio.probed] == ["audio/wav"]
    asr = portal.container.settings.media.asr
    assert {threads for _, _, threads in bench.speech.calls} == {asr.threads - asr.chat_threads}

    # После обработки остаются расшифровка и сжатый звук: исходника и временных файлов нет.
    row = await _row(portal, document_id)
    assert (row.storage_key, row.seconds_done) == (None, 70)
    (audio,) = _media_files(portal)
    assert audio.read_bytes() == OPUS
    assert row.audio_key == f"media/{await portal.user_id('ivanov')}/{audio.name}"

    found = await bench.knowledge.retrieve("отрезок 2", await portal.user_id("ivanov"), SHARED)
    source = found.sources[0]
    assert (source.timecode_seconds, source.page, source.image_id) == (30, None, None)
    assert source.quote == "Расшифровка-заглушка, отрезок 2."
    assert "[1] совещание.wav, запись, 00:00:30\n<фрагмент>" in found.context

    text_path = f"/api/kb/documents/{document_id}/text"
    by_time = (await client.get(text_path, params={"timecode": 45})).json()
    assert by_time == {
        "page": 2,
        "page_count": 3,
        "recognized": True,
        "segments": [
            {"text": "Расшифровка-заглушка, отрезок 2.", "highlight": False, "start_seconds": 30}
        ],
        "timecode_seconds": 45,
        "image": None,
    }
    by_fragment = (
        await client.get(text_path, params={"fragment_id": str(source.fragment_id), "timecode": 5})
    ).json()
    assert (by_fragment["page"], by_fragment["timecode_seconds"]) == (2, 30)
    assert [segment["highlight"] for segment in by_fragment["segments"]] == [True]
    first = (await client.get(text_path)).json()
    assert (first["page"], first["timecode_seconds"]) == (1, 0)
    assert _code(await client.get(text_path, params={"timecode": -1})) == (422, "validation_error")
    # Значение, которое не помещается в столбец базы, — ошибка параметра, а не сбой запроса.
    for params in ({"timecode": 2**31}, {"timecode": 2**63}, {"page": 2**31}, {"page": 10**30}):
        assert _code(await client.get(text_path, params=params)) == (422, "validation_error")
    assert (await client.get(text_path, params={"timecode": 2**31 - 1})).status_code == 200


async def test_lease_renewal_does_not_take_a_completed_job_for_a_withdrawn_one(
    recording_bench: MakeBench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Продление не нашло строки задания, потому что оно выполнено: уборка идёт до конца."""
    bench = await recording_bench(worker={"lease_seconds": 0.3})  # продление — раз в 0,1 с
    portal = bench.portal
    client = await portal.employee()
    document_id = await sent(client)
    original = bench.index.prune_document
    calls = 0

    async def slow_after_commit(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # уборка прежних точек — уже после фиксации завершения
            await asyncio.sleep(0.4)
        await original(*args)

    monkeypatch.setattr(bench.index, "prune_document", slow_after_commit)
    with caplog.at_level(logging.INFO, logger="portal.worker.loop"):
        assert await bench.worker.run_once()

    messages = [record.getMessage() for record in caplog.records]
    assert "job done" in messages and "job withdrawn" not in messages
    assert calls == 2  # уборка не повторялась
    row = await _row(portal, document_id)
    # Исходник готовой записи удалён сразу, а не ближайшей уборкой воркера.
    assert (row.status, row.storage_key) == ("ready", None)
    assert [path.read_bytes() for path in _media_files(portal)] == [OPUS]


async def test_audio_is_served_by_ranges_and_only_to_those_who_see_the_recording(
    recording_bench: MakeBench,
) -> None:
    bench = await recording_bench()
    portal = bench.portal
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    admin = portal.client()
    await portal.onboard(admin, "boss", "admin")
    document_id = await sent(ivanov)
    text_id = await sent(ivanov, WAV + b"other", scope="shared")
    await bench.drain()
    path = f"/api/kb/documents/{document_id}/audio"

    whole = await ivanov.get(path)
    assert (whole.status_code, whole.content) == (200, OPUS)
    assert whole.headers["content-type"] == "audio/ogg"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["x-content-type-options"] == "nosniff"
    assert whole.headers["content-disposition"] == "inline"

    for header, content_range, expected in (
        ("bytes=10-19", "bytes 10-19/100", OPUS[10:20]),
        ("bytes=90-", "bytes 90-99/100", OPUS[90:]),
        ("bytes=-5", "bytes 95-99/100", OPUS[95:]),
        ("bytes=95-500", "bytes 95-99/100", OPUS[95:]),
    ):
        part = await ivanov.get(path, headers={"Range": header})
        assert (part.status_code, part.content) == (206, expected), header
        assert part.headers["content-range"] == content_range
        assert part.headers["content-length"] == str(len(expected))
    for header in ("bytes=100-", "bytes=500-600", "bytes=-0"):
        outside = await ivanov.get(path, headers={"Range": header})
        assert _code(outside) == (416, "range_not_satisfiable"), header
        assert outside.headers["content-range"] == "bytes */100"
    # Несколько диапазонов и нечитаемый заголовок игнорируются: файл целиком.
    # Число из тысяч цифр — тоже нечитаемый заголовок, а не сбой разбора.
    digits = "9" * 5000
    for header in (
        "bytes=0-1,5-6",
        "items=0-1",
        "bytes=5-1x",
        f"bytes={digits}-",
        f"bytes=-{digits}",
        f"bytes=0-{digits}",
    ):
        ignored = await ivanov.get(path, headers={"Range": header})
        assert (ignored.status_code, ignored.content) == (200, OPUS), header

    # Звук и расшифровка чужой личной записи не существуют — и для администратора тоже.
    for stranger in (petrov, admin):
        assert (await stranger.get(path)).status_code == 404
        assert (await stranger.get(path, headers={"Range": "bytes=0-9"})).status_code == 404
        assert (await stranger.get(f"/api/kb/documents/{document_id}/text")).status_code == 404
    assert (await portal.client().get(path)).status_code == 401
    # Общая запись слышна всем; у документа, который не запись, звука нет.
    assert (await petrov.get(f"/api/kb/documents/{text_id}/audio")).status_code == 200

    assert (await ivanov.delete(f"/api/kb/documents/{document_id}")).status_code == 204
    assert (await ivanov.get(path)).status_code == 404
    await bench.drain()
    assert len(_media_files(portal)) == 1  # остался звук второй записи


async def test_refusals_come_before_the_body_and_leave_no_file(recording_bench: MakeBench) -> None:
    bench = await recording_bench()
    portal = bench.portal
    client = await portal.employee()
    admin = portal.client()
    await portal.onboard(admin, "boss", "admin")
    read: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        read.append(WAV)
        yield WAV

    async def refusal(sender: httpx.AsyncClient, **params: Any) -> tuple[int, str]:
        return _code(await send(sender, body(), declared=len(WAV), **params))

    # Право на коллекцию проверяется раньше параметров строки запроса (§1.6).
    assert await refusal(client, scope="ministries", name=None) == (403, "forbidden")
    assert await refusal(client, scope=None) == (422, "validation_error")
    assert await refusal(client, scope="everyone") == (422, "validation_error")
    assert await refusal(client, name=None) == (422, "validation_error")
    assert await refusal(client, tags=["т" * 51]) == (422, "validation_error")
    # Без заявленной длины тело не принимается: по ней резервируется место.
    assert _code(await send(client, body())) == (411, "length_required")
    assert read == [] and _media_files(portal) == []

    # Не запись — приём обрывается; повтор файла в той же коллекции — отказ; файлов не остаётся.
    assert _code(await send(client, b"%PDF-1.4\n" + bytes(100))) == (415, "unsupported_file_type")
    document_id = await sent(client)
    duplicate = await send(client, name="то же самое.wav")
    assert _code(duplicate) == (409, "duplicate_document")
    assert duplicate.json()["error"]["details"]["document"]["id"] == document_id
    assert len(_media_files(portal)) == 1
    # В другой коллекции тот же файл — не повтор; «Министерства» ведёт администратор.
    assert (await send(admin, scope="ministries")).status_code == 201
    events = [event for event, _ in await portal.audit_events()]
    assert events.count("document_uploaded") == 2


async def test_recording_is_not_added_when_the_session_ended_during_the_upload(
    recording_bench: MakeBench,
) -> None:
    """Тело принималось долго: блокировка и смена роли за это время удаляют сессию (§1.6)."""
    bench = await recording_bench()
    portal = bench.portal
    await portal.employee()
    user = CurrentUser(await portal.user_id("ivanov"), "employee", "Иванов", None)
    kb = portal.container.kb

    async def body() -> AsyncIterator[bytes]:
        yield WAV

    recording = await portal.container.media_storage.receive(user.id, body(), len(WAV))
    target = kb.recording_target(user, "shared", "совещание.wav", ())
    with pytest.raises(AppError) as refusal:
        await kb.add_recording(user, uuid4(), target, recording)

    assert (refusal.value.status, refusal.value.code) == (401, "unauthenticated")
    assert _media_files(portal) == []
    assert await portal.rows("SELECT 1 FROM kb_documents") == []
    assert await portal.rows("SELECT 1 FROM kb_jobs") == []


async def test_disabled_feature_refuses_uploads(recording_bench: MakeBench) -> None:
    bench = await recording_bench(enabled=False)
    client = await bench.portal.employee()
    assert _code(await send(client)) == (403, "feature_disabled")
    assert _media_files(bench.portal) == []


async def test_quota_of_one_employee_does_not_take_the_place_of_others(
    recording_bench: MakeBench,
) -> None:
    bench = await recording_bench(quota_bytes=1000, user_quota_share=0.25)
    portal = bench.portal
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    document_id = await sent(ivanov, WAV + bytes(80))  # 196 из 250 байт сотрудника

    over = await send(ivanov, WAV)
    assert _code(over) == (507, "media_quota_exceeded")
    assert over.json()["error"]["details"] == {"reason": "user"}
    assert len(_media_files(portal)) == 1
    assert (await send(petrov, WAV)).status_code == 201

    # Место освобождает удаление своей записи — когда воркер стёр её файлы.
    assert (await ivanov.delete(f"/api/kb/documents/{document_id}")).status_code == 204
    while await bench.worker.run_once():
        pass
    assert (await send(ivanov, WAV + b"new")).status_code == 201


@pytest.mark.parametrize(
    ("fault", "error_code", "duration"),
    [
        ("media_invalid", "media_invalid", None),
        ("media_no_audio", "media_no_audio", None),
        ("too_long", "media_too_long", 121),
        ("silent", "no_text", 70),
    ],
)
async def test_recording_that_cannot_be_processed_loses_its_source_at_once(
    recording_bench: MakeBench, fault: str, error_code: str, duration: int | None
) -> None:
    bench = await recording_bench(max_minutes=2)
    portal = bench.portal
    client = await portal.employee()
    if fault == "too_long":
        bench.audio.duration_ms = 120_500
    elif fault == "silent":
        bench.speech.silent = True
    else:
        bench.audio.rejection = fault  # type: ignore[assignment]
    document_id = await sent(client)
    await bench.drain()

    document = await _document(client, document_id)
    assert (document["status"], document["error_code"]) == ("error", error_code)
    assert document["duration_seconds"] == duration
    row = await _row(portal, document_id)
    assert (row.storage_key, row.audio_key) == (None, None)
    assert _media_files(portal) == [] and await portal.rows("SELECT 1 FROM kb_jobs") == []

    # Обработать нечем: ни повторная обработка, ни `portal reindex` запись не берут.
    retry = await client.post(f"/api/kb/documents/{document_id}/retry")
    assert _code(retry) == (409, "source_unavailable")
    assert await bench.reindexer.run(only_errors=True, recreate_collection=False) == 0
    assert (await _row(portal, document_id)).status == "error"
    # Загрузить ту же запись заново можно только после удаления прежней строки.
    assert _code(await send(client)) == (409, "duplicate_document")


async def test_interrupted_transcription_continues_from_the_saved_parts(
    recording_bench: MakeBench,
) -> None:
    bench = await recording_bench()
    portal = bench.portal
    client = await portal.employee()
    bench.speech.fail_from_ms = 30_000
    document_id = await sent(client)

    assert await bench.drain() == 1
    row = await _row(portal, document_id)
    assert (row.status, row.seconds_done, row.duration_seconds) == ("queued", 30, 70)
    assert row.storage_key is not None and row.audio_key is None
    assert len(_media_files(portal)) == 1  # временного каталога задания не осталось

    bench.speech.fail_from_ms = None
    portal.clock.advance(hours=1)
    assert await bench.drain() == 1
    assert (await _row(portal, document_id)).status == "ready"
    # Первая часть заново не расшифровывалась.
    assert [start for start, _, _ in bench.speech.calls] == [0, 30_000, 60_000]

    # Готовая запись переиндексируется по сохранённой расшифровке, без исходника.
    probes = len(bench.audio.probed)
    assert await bench.reindexer.run(only_errors=False, recreate_collection=False) == 1
    await bench.drain()
    assert (await _row(portal, document_id)).status == "ready"
    assert len(bench.audio.probed) == probes and len(bench.speech.calls) == 3
    assert (await client.get(f"/api/kb/documents/{document_id}/audio")).status_code == 200


async def test_failed_recording_keeps_its_source_until_the_term_expires(
    recording_bench: MakeBench,
) -> None:
    bench = await recording_bench(worker={"max_attempts": 1})
    portal = bench.portal
    client = await portal.employee()
    bench.speech.fail_from_ms = 30_000
    expired_id = await sent(client)
    retried_id = await sent(client, WAV + b"second")
    await bench.drain()
    for document_id in (expired_id, retried_id):
        document = await _document(client, document_id)
        assert (document["status"], document["error_code"]) == ("error", "transcription_failed")
    assert len(_media_files(portal)) == 2

    # Пока исходник на месте, повторная обработка доводит запись до конца.
    bench.speech.fail_from_ms = None
    assert (await client.post(f"/api/kb/documents/{retried_id}/retry")).status_code == 200
    await bench.drain()
    assert (await _row(portal, retried_id)).status == "ready"

    # Срок считается от загрузки; время сдвигается в строке, чтобы сессия теста не истекла.
    ttl_hours = portal.container.settings.media.failed_source_ttl_hours
    older = "UPDATE kb_documents SET created_at = created_at - make_interval(hours => :hours)"
    await portal.execute(older, hours=ttl_hours - 1)
    await bench.janitor.sweep()
    assert (await _row(portal, expired_id)).storage_key is not None
    await portal.execute(older, hours=2)
    await bench.janitor.sweep()
    row = await _row(portal, expired_id)
    assert (row.status, row.error_code, row.storage_key) == ("error", "source_expired", None)
    assert len(_media_files(portal)) == 1  # только сжатый звук готовой записи
    retry = await client.post(f"/api/kb/documents/{expired_id}/retry")
    assert _code(retry) == (409, "source_unavailable")


def _age(path: Path, modified: datetime) -> None:
    os.utime(path, (modified.timestamp(), modified.timestamp()))


async def test_sweep_removes_only_old_files_that_no_row_refers_to(
    recording_bench: MakeBench,
) -> None:
    bench = await recording_bench()
    portal = bench.portal
    client = await portal.employee()
    owner = await portal.user_id("ivanov")
    ready_id = await sent(client)
    await bench.drain()
    queued_id = await sent(client, WAV + b"queued")
    dialog_id = await new_dialog(client)
    attachment = await client.post(
        f"/api/dialogs/{dialog_id}/attachments/media", params={"file_name": "a.wav"}, content=WAV
    )
    assert attachment.status_code == 201, attachment.text

    directory = portal.container.settings.files.root / "media" / str(owner)
    # Сбой после фиксации: исходник готовой записи остался на диске.
    leftover = f"media/{owner}/{uuid4()}"
    (directory / leftover.rsplit("/", 1)[1]).write_bytes(WAV)
    await portal.execute(
        "UPDATE kb_documents SET storage_key = :key WHERE id = :id", key=leftover, id=ready_id
    )
    old_orphan, fresh_orphan = directory / str(uuid4()), directory / str(uuid4())
    upload = directory / f"{uuid4()}.part"
    for path in (old_orphan, fresh_orphan, upload):
        path.write_bytes(b"x" * 10)
    long_ago = datetime.fromtimestamp(0).astimezone()
    for path in _media_files(portal):
        if path != fresh_orphan:
            _age(path, long_ago)

    await bench.janitor.sweep()

    left = set(_media_files(portal))
    assert old_orphan not in left and not (directory / leftover.rsplit("/", 1)[1]).exists()
    # Свежий файл (строка могла ещё не зафиксироваться) и недописанная загрузка не тронуты.
    assert {fresh_orphan, upload} <= left
    # Файлы, на которые ссылаются строки, целы: сжатый звук, исходник записи в очереди, вложение.
    assert len(left) == 5
    ready, queued = await _row(portal, ready_id), await _row(portal, queued_id)
    assert (ready.storage_key, ready.status) == (None, "ready") and ready.audio_key is not None
    assert queued.storage_key is not None
    assert (await client.get(f"/api/kb/documents/{ready_id}/audio")).status_code == 200


async def test_deleting_a_queued_recording_erases_its_source(recording_bench: MakeBench) -> None:
    bench = await recording_bench()
    portal = bench.portal
    client = await portal.employee()
    document_id = await sent(client)
    assert (await client.delete(f"/api/kb/documents/{document_id}")).status_code == 204

    assert await bench.drain() == 1
    assert _media_files(portal) == [] and await portal.rows("SELECT 1 FROM kb_documents") == []
    assert bench.audio.probed == []
