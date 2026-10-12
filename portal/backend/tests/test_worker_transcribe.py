"""Задание `transcribe`: расшифровка записи во вложении чата на своём месте воркера (§11.7)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from portal.core.settings import Settings
from portal.worker.queue import SqlTranscriptionQueue
from tests.conftest import close_portal, make_kb_bench
from tests.kb_support import KbBench, added, kb_settings
from tests.support import Portal, new_dialog

pytestmark = pytest.mark.anyio

WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(100)
TRANSCRIPT = " ".join(f"Расшифровка-заглушка, отрезок {number}." for number in (1, 2, 3))


@pytest.fixture
async def chat_bench(settings: Settings, tmp_path: Path) -> AsyncIterator[KbBench]:
    """Стенд воркера с настоящей очередью расшифровки; свободное место тома не проверяется."""
    media = settings.media.model_copy(update={"min_free_bytes": 0})
    prepared = kb_settings(settings, tmp_path).model_copy(update={"media": media})
    built = await make_kb_bench(prepared)
    yield built
    await close_portal(built.portal)


def _queue(portal: Portal) -> SqlTranscriptionQueue:
    return SqlTranscriptionQueue(portal.container.engine, portal.clock)


async def _recording(
    bench: KbBench, client: httpx.AsyncClient, content: bytes = WAV
) -> tuple[str, UUID]:
    """Загрузить запись в чат и поставить её расшифровку в настоящую очередь."""
    dialog_id = await new_dialog(client)
    response = await client.post(
        f"/api/dialogs/{dialog_id}/attachments/media",
        params={"file_name": "совещание.wav"},
        content=content,
    )
    assert response.status_code == 201, response.text
    attachment_id = UUID(response.json()["id"])
    await _queue(bench.portal).enqueue(attachment_id)
    return f"/api/dialogs/{dialog_id}/attachments/{attachment_id}", attachment_id


async def _state(client: httpx.AsyncClient, path: str) -> tuple[str, str | None, int | None]:
    body = (await client.get(path)).json()
    transcription = body["transcription"]
    return transcription["status"], transcription["error_code"], body["duration_seconds"]


def _media_files(portal: Portal) -> list[Path]:
    root = portal.container.settings.files.root / "media"
    return [path for path in root.rglob("*") if path.is_file()]


async def _jobs(portal: Portal) -> list[Any]:
    return await portal.rows("SELECT kind, priority, attempts, attachment_id FROM kb_jobs")


async def test_attachment_is_transcribed_on_its_own_slot(chat_bench: KbBench) -> None:
    bench, portal = chat_bench, chat_bench.portal
    client = await portal.employee()
    path, attachment_id = await _recording(bench, client)
    await added(client, "заметка.txt", "Документ базы знаний".encode())
    jobs = sorted((job.kind, job.priority) for job in await _jobs(portal))
    assert jobs == [("index", 20), ("transcribe", 10)]

    # Основное место задание `transcribe` не берёт, место под вложения — ничего другого.
    assert await bench.drain() == 1
    assert [job.kind for job in await _jobs(portal)] == ["transcribe"]
    assert await bench.worker.run_transcription_once()
    assert not await bench.worker.run_transcription_once()

    assert await _state(client, path) == ("ready", None, 70)
    (row,) = await portal.rows(
        "SELECT text_content FROM attachments WHERE id = :id", id=attachment_id
    )
    assert row.text_content == TRANSCRIPT
    # Исходник, временный каталог и задание удалены; сжатый звук у вложения не создаётся.
    assert _media_files(portal) == [] and await _jobs(portal) == []
    asr = portal.container.settings.media.asr
    assert bench.speech.calls == [(0, 70_000, asr.chat_threads)]
    assert [media_type for _, media_type in bench.audio.probed] == ["audio/wav"]


async def test_failure_is_retried_once_at_once_and_then_becomes_an_error(
    chat_bench: KbBench,
) -> None:
    bench, portal = chat_bench, chat_bench.portal
    client = await portal.employee()
    recovered, _ = await _recording(bench, client)
    bench.speech.failures = 1
    assert await bench.worker.run_transcription_once()
    assert (await _state(client, recovered))[0] == "processing"
    assert len(_media_files(portal)) == 1  # исходник ждёт повторной попытки
    # Повтор — сразу, без паузы: часы теста стоят.
    assert await bench.worker.run_transcription_once()
    assert await _state(client, recovered) == ("ready", None, 70)

    failed, _ = await _recording(bench, client, WAV + b"second")
    bench.audio.failures = 2
    assert await bench.worker.run_transcription_once()
    assert await bench.worker.run_transcription_once()
    assert await _state(client, failed) == ("error", "transcription_failed", None)
    assert _media_files(portal) == [] and await _jobs(portal) == []


async def test_job_taken_for_the_third_time_is_not_executed(chat_bench: KbBench) -> None:
    """Процесс воркера погибал на обеих попытках: счётчик вырос, а исхода нет (§11.1)."""
    bench, portal = chat_bench, chat_bench.portal
    client = await portal.employee()
    path, _ = await _recording(bench, client)
    await portal.execute("UPDATE kb_jobs SET attempts = 2")

    assert await bench.worker.run_transcription_once()
    assert await _state(client, path) == ("error", "internal_error", None)
    assert bench.audio.probed == [] and _media_files(portal) == [] and await _jobs(portal) == []


@pytest.mark.parametrize(
    ("fault", "outcome"),
    [
        ("media_invalid", ("error", "media_invalid", None)),
        ("media_no_audio", ("error", "media_no_audio", None)),
        ("too_long", ("error", "media_too_long", 1201)),
        ("silent", ("error", "no_text", 70)),
    ],
)
async def test_rejected_attachment_gets_its_reason_and_loses_the_file(
    chat_bench: KbBench, fault: str, outcome: tuple[str, str, int | None]
) -> None:
    bench, portal = chat_bench, chat_bench.portal
    client = await portal.employee()
    if fault == "too_long":
        bench.audio.duration_ms = 20 * 60_000 + 500
    elif fault == "silent":
        bench.speech.silent = True
    else:
        bench.audio.rejection = fault  # type: ignore[assignment]
    path, _ = await _recording(bench, client)

    assert await bench.worker.run_transcription_once()
    assert await _state(client, path) == outcome
    assert _media_files(portal) == [] and await _jobs(portal) == []
    assert bench.speech.calls == [] or fault == "silent"


async def test_deleted_attachment_takes_its_job_away(chat_bench: KbBench) -> None:
    bench, portal = chat_bench, chat_bench.portal
    client = await portal.employee()
    path, attachment_id = await _recording(bench, client)
    queue = _queue(portal)
    # Повторная постановка второго задания не создаёт.
    await queue.enqueue(attachment_id)
    assert len(await _jobs(portal)) == 1
    assert await queue.pending([attachment_id, UUID(int=1)]) == {attachment_id}

    assert (await client.delete(path)).status_code == 204
    assert await queue.pending([attachment_id]) == set()
    assert not await bench.worker.run_transcription_once()
    assert _media_files(portal) == []


async def test_worker_start_returns_abandoned_attachments_to_the_queue(
    chat_bench: KbBench,
) -> None:
    """После аварийной остановки вложение не остаётся «расшифровывается» без воркера."""
    bench, portal = chat_bench, chat_bench.portal
    client = await portal.employee()
    path, attachment_id = await _recording(bench, client)
    await portal.execute("UPDATE attachments SET transcription_status = 'processing'")
    await portal.execute(
        "UPDATE kb_jobs SET attempts = 1, locked_until = now() - interval '1 year'"
    )
    scratch = bench.media.scratch(attachment_id)
    (scratch / "audio.wav").write_bytes(b"leftover")
    stop = asyncio.Event()
    stop.set()

    await bench.worker.run(stop)

    assert (await _state(client, path))[0] == "queued"
    # Временные каталоги прежнего процесса убраны, исходник на месте.
    assert [file.parent.name for file in _media_files(portal)] == [
        str(await portal.user_id("ivanov"))
    ]
