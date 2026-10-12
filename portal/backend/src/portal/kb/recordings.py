"""Записи в базе знаний: проверка, расшифровка частями, сжатый звук (docs/portal-api.md §8.7).

Сценарий работает через порты: хранилище записей, ffmpeg (`AudioTools`), распознавание
речи. Текст расшифровки в журнал не попадает.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID

from portal.core.settings import MediaSettings
from portal.files.ports import MediaStorage, RecordingType
from portal.kb.domain import ErrorCode, Job, KbDocument, PageText
from portal.kb.outcomes import JobAbandonedError, PermanentIndexError
from portal.kb.store import KbUnitOfWorkFactory
from portal.media.ports import AudioInfo, AudioTools, MediaRejectedError
from portal.media.transcription import (
    SpeechPool,
    part_bounds,
    transcript_part,
    whole_seconds,
)

_MS = 1000
_SOURCE = "source"
_WAV = "audio.wav"
_OPUS = "audio.ogg"

# Сохранить часть расшифровки: (документ, страница, объём текста до неё) → объём с ней.
type SavePage = Callable[[UUID, PageText, int], Awaitable[int]]


class RecordingProcessor:
    """Шаг «Страницы» задания индексации у записи: проверка → звук → части → сжатый звук."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        media: MediaStorage,
        audio: AudioTools,
        speech: SpeechPool,
        settings: MediaSettings,
    ) -> None:
        """Получить зависимости явно; `speech` — очередь распознавания заданий базы знаний."""
        self._uow_factory = uow_factory
        self._media = media
        self._audio = audio
        self._speech = speech
        self._settings = settings

    async def transcribe(self, job: Job, document: KbDocument, save_page: SavePage) -> None:
        """Расшифровать запись частями и сжать её звук.

        Идемпотентно: уже сохранённые части заново не расшифровываются; у записи с
        готовым сжатым звуком шаг пропускается целиком — исходник ей не нужен. Причина в
        файле — `PermanentIndexError`, исходник при этом удаляется сразу.
        """
        if document.audio_key is not None:
            return
        # Запись без исходника и без сжатого звука в очередь не попадает (§8.1, §13.4).
        assert document.storage_key is not None
        media_type = cast(RecordingType, document.media_type)
        scratch = await asyncio.to_thread(self._media.scratch, job.id)
        try:
            # Жёсткая ссылка: вход ffmpeg — один файл в пустом каталоге задания.
            source = scratch / _SOURCE
            await asyncio.to_thread(os.link, self._media.path(document.storage_key), source)
            info = await self._checked(document.id, source, media_type)
            wav = scratch / _WAV
            await self._audio.to_wav(source, media_type, wav)
            await self._parts(job, document.id, wav, info.duration_ms, save_page)
            await self._compress(document, wav, scratch / _OPUS)
        finally:
            await asyncio.shield(self._media.drop_scratch(job.id))

    async def _checked(
        self, document_id: UUID, source: Path, media_type: RecordingType
    ) -> AudioInfo:
        """Проверка записи портом `AudioTools`; отказ — постоянная ошибка документа."""
        try:
            info = await self._audio.probe(source, media_type)
        except MediaRejectedError as rejection:
            await self._reject(document_id, rejection.code)
        async with self._uow_factory() as uow:
            if await uow.documents.lock(document_id) is None:
                raise JobAbandonedError
            await uow.documents.set_duration(document_id, whole_seconds(info.duration_ms))
        if info.duration_ms > self._settings.max_minutes * 60 * _MS:
            await self._reject(document_id, "media_too_long")
        return info

    async def _reject(self, document_id: UUID, code: ErrorCode) -> NoReturn:
        """Повторная обработка такой записи не поможет: исходник удаляется сразу (§8.7)."""
        await self.drop_source(document_id)
        raise PermanentIndexError(code)

    async def _parts(
        self, job: Job, document_id: UUID, wav: Path, duration_ms: int, save_page: SavePage
    ) -> None:
        """Расшифровать ещё не сохранённые части; каждая готовая сразу сохраняется."""
        async with self._uow_factory() as uow:
            stored = await uow.documents.stored_pages(document_id)
        chars = sum(stored.values())
        bounds = part_bounds(duration_ms, self._settings.part_seconds * _MS)
        for number, (start_ms, end_ms) in enumerate(bounds, 1):
            if number in stored:
                continue
            async with self._uow_factory() as uow:
                if not await uow.jobs.exists(job.id):
                    raise JobAbandonedError
            part = transcript_part(await self._speech.transcribe(wav, start_ms, end_ms))
            chars = await save_page(
                document_id, PageText(number, part.text, True, start_ms, part.marks), chars
            )
            async with self._uow_factory() as uow:
                await uow.documents.set_seconds_done(document_id, end_ms // _MS)
        if chars == 0:
            # Все части расшифрованы, и ни в одной нет речи: повтор дал бы тот же итог.
            await self._reject(document_id, "no_text")

    async def _compress(self, document: KbDocument, wav: Path, opus: Path) -> None:
        """Сжатый звук для прослушивания; его ключ — признак завершённой расшифровки."""
        await self._audio.to_opus(wav, opus)
        key = self._media.new_key(document.owner_id)
        await asyncio.to_thread(os.replace, opus, self._media.path(key))
        try:
            async with self._uow_factory() as uow:
                if await uow.documents.lock(document.id) is None:
                    raise JobAbandonedError
                await uow.documents.set_audio_key(document.id, key)
        except BaseException:
            await self._media.delete(key)
            raise

    async def drop_source(self, document_id: UUID) -> None:
        """Удалить исходный файл записи и отметить это в строке документа."""
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(document_id, include_deleted=True)
            if document is None or document.storage_key is None:
                return
            await self._media.delete(document.storage_key)
            await uow.documents.clear_source(document_id)
