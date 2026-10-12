"""Задание `transcribe`: расшифровка записи во вложении чата (docs/portal-api.md §11.7).

Сценарий работает через порты: вложение глазами воркера (`AttachmentTranscripts`),
хранилище записей, ffmpeg, распознавание речи. Текст расшифровки в журнал не попадает.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable

from portal.core.logging import code_locations
from portal.core.ports import Clock
from portal.core.settings import MediaSettings
from portal.dialogs.ports import AttachmentTranscripts, RecordingAttachment, TranscriptionError
from portal.files.ports import MediaStorage
from portal.kb.domain import TRANSCRIBE_MAX_ATTEMPTS, TranscriptionJob
from portal.kb.outcomes import JobAbandonedError
from portal.kb.store import KbUnitOfWorkFactory
from portal.media.ports import (
    AudioTools,
    AudioToolsError,
    MediaRejectedError,
    SpeechRecognitionError,
)
from portal.media.transcription import SpeechPool, transcript_part, whole_seconds

logger = logging.getLogger(__name__)

_SOURCE = "source"
_WAV = "audio.wav"


class _RefusedError(Exception):
    """Причина в самой записи: повтор не поможет."""

    def __init__(self, code: TranscriptionError, duration_seconds: int | None) -> None:
        super().__init__(code)
        self.code: TranscriptionError = code
        self.duration_seconds = duration_seconds


class AttachmentTranscriber:
    """Выполняет задание `transcribe` и записывает его исход во вложение."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        transcripts: AttachmentTranscripts,
        media: MediaStorage,
        audio: AudioTools,
        speech: SpeechPool,
        clock: Clock,
        settings: MediaSettings,
    ) -> None:
        """Получить зависимости явно; `speech` — очередь распознавания места под вложения."""
        self._uow_factory = uow_factory
        self._transcripts = transcripts
        self._media = media
        self._audio = audio
        self._speech = speech
        self._clock = clock
        self._settings = settings

    async def run(self, job: TranscriptionJob) -> None:
        """Расшифровать запись вложения; в любом исходе, кроме повтора, исходник удаляется.

        Сбой — одна повторная попытка сразу, без паузы. Задание, взятое в третий раз
        (процесс воркера погибал на обеих попытках), не выполняется: `internal_error`.
        """
        fields = {"job_id": str(job.id), "kind": "transcribe", "attempt": job.attempts}
        attachment_id = job.attachment_id
        recording = await self._transcripts.begin(attachment_id)
        if recording is None:
            # Вложения уже нет либо его расшифровка завершена: выполнять нечего.
            await self._finish(job)
            return
        if job.attempts > TRANSCRIBE_MAX_ATTEMPTS:
            await self._close(
                job, recording, self._transcripts.fail(attachment_id, "internal_error", None)
            )
            return
        try:
            text, seconds = await self._transcribe(job, recording)
        except JobAbandonedError:
            # Вложение удалено: его файл убирает удаление вложения (§9.16).
            logger.info("job abandoned", extra=fields)
        except _RefusedError as refusal:
            await self._close(
                job,
                recording,
                self._transcripts.fail(attachment_id, refusal.code, refusal.duration_seconds),
            )
            logger.info("recording rejected", extra={**fields, "error_code": refusal.code})
        except Exception as error:
            logger.warning(
                "job failed",
                extra={
                    **fields,
                    "error_type": type(error).__name__,
                    "trace": code_locations(error),
                },
            )
            if job.attempts < TRANSCRIBE_MAX_ATTEMPTS:
                async with self._uow_factory() as uow:
                    await uow.jobs.postpone(job.id, self._clock.now())
                return
            speech_failed = isinstance(error, AudioToolsError | SpeechRecognitionError)
            code: TranscriptionError = "transcription_failed" if speech_failed else "internal_error"
            await self._close(job, recording, self._transcripts.fail(attachment_id, code, None))
        else:
            await self._close(
                job, recording, self._transcripts.complete(attachment_id, text, seconds)
            )
            logger.info("job done", extra=fields)

    async def _transcribe(
        self, job: TranscriptionJob, recording: RecordingAttachment
    ) -> tuple[str, int]:
        """Проверка, извлечение звука, расшифровка целиком: текст и длительность в секундах."""
        scratch = await asyncio.to_thread(self._media.scratch, job.id)
        try:
            source = scratch / _SOURCE
            await asyncio.to_thread(os.link, self._media.path(recording.storage_key), source)
            try:
                info = await self._audio.probe(source, recording.media_type)
            except MediaRejectedError as rejection:
                raise _RefusedError(rejection.code, None) from None
            seconds = whole_seconds(info.duration_ms)
            if info.duration_ms > self._settings.chat_max_minutes * 60_000:
                raise _RefusedError("media_too_long", seconds)
            wav = scratch / _WAV
            await self._audio.to_wav(source, recording.media_type, wav)
            async with self._uow_factory() as uow:
                # Вложение удалили — задание снято каскадом: модель речи на него не тратится.
                if not await uow.jobs.exists(job.id):
                    raise JobAbandonedError
            text = transcript_part(await self._speech.transcribe(wav, 0, info.duration_ms)).text
            if not text:
                raise _RefusedError("no_text", seconds)
            return text, seconds
        finally:
            await asyncio.shield(self._media.drop_scratch(job.id))

    async def _close(
        self, job: TranscriptionJob, recording: RecordingAttachment, outcome: Awaitable[None]
    ) -> None:
        """Записать исход во вложение, удалить исходный файл и само задание (§11.7, шаг 5)."""
        await outcome
        await self._media.delete(recording.storage_key)
        await self._finish(job)

    async def _finish(self, job: TranscriptionJob) -> None:
        async with self._uow_factory() as uow:
            await uow.jobs.finish(job.id)
