"""Цикл очереди заданий: аренда, продление, повторы, два набора мест (docs/portal-api.md §11).

Задание идемпотентно и переживает перезапуск: брошенное берётся заново по истечении
аренды, а при штатной остановке возвращается в очередь сразу.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from functools import partial
from typing import Any
from uuid import UUID

from portal.core.logging import code_locations
from portal.core.ports import Clock
from portal.core.settings import KbWorkerSettings
from portal.dialogs.ports import AttachmentTranscripts
from portal.files.ports import MediaStorage
from portal.kb.domain import ErrorCode, Job, TranscriptionJob
from portal.kb.explaining import Contextualizer
from portal.kb.indexing import Indexer
from portal.kb.outcomes import JobAbandonedError, PermanentIndexError
from portal.kb.ports import RecognitionFailedError, VectorIndex
from portal.kb.store import KbUnitOfWorkFactory
from portal.media.ports import AudioToolsError, SpeechRecognitionError
from portal.worker.sweep import RecordingJanitor
from portal.worker.transcribe import AttachmentTranscriber

logger = logging.getLogger(__name__)

# Аренда продлевается не реже раза в треть её срока (§11.1).
_RENEWALS_PER_LEASE = 3
# Задания обработки: у одного документа одновременно выполняется не больше одного (§11.1).
_PROCESSING_KINDS = ("index", "reprocess")
# Уборка исходников записей — при старте и не реже раза в час (§8.7).
_SWEEP_INTERVAL_SECONDS = 3600


def _job_fields(job: Job) -> dict[str, object]:
    """Поля задания для журнала: только идентификаторы и счётчики."""
    return {
        "job_id": str(job.id),
        "document_id": str(job.document_id),
        "kind": job.kind,
        "attempt": job.attempts,
    }


def _exhausted_code(error: Exception) -> ErrorCode:
    """Причина ошибки документа, когда попытки задания исчерпаны (§11.2)."""
    if isinstance(error, RecognitionFailedError):
        return "recognition_failed"
    if isinstance(error, AudioToolsError | SpeechRecognitionError):
        return "transcription_failed"
    return "internal_error"


class Worker:
    """Берёт задания из `kb_jobs`; у воркера два независимых набора мест (§11.1).

    Основные (`concurrency`) берут все задания, кроме `transcribe`; места под вложения
    чата (`chat_slots`) — только `transcribe`.
    """

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        indexer: Indexer,
        contextualizer: Contextualizer,
        attachments: AttachmentTranscriber,
        transcripts: AttachmentTranscripts,
        media: MediaStorage,
        janitor: RecordingJanitor,
        index: VectorIndex,
        clock: Clock,
        settings: KbWorkerSettings,
    ) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._indexer = indexer
        self._contextualizer = contextualizer
        self._attachments = attachments
        self._transcripts = transcripts
        self._media = media
        self._janitor = janitor
        self._index = index
        self._clock = clock
        self._settings = settings
        self._lease = timedelta(seconds=settings.lease_seconds)
        # Задания, которые сняты, пока выполнялись: их аренду возвращать незачем.
        self._withdrawn: set[UUID] = set()
        # Документы, по которым идёт `index` или `reprocess`: второе такое задание ждёт,
        # пока не остановится первое, даже снятое (§11.1).
        self._processing: set[UUID] = set()

    async def run(self, stop: asyncio.Event) -> None:
        """Работать, пока не выставлен `stop`; затем вернуть взятые задания в очередь.

        Перед началом создаётся коллекция Qdrant; коллекция с чужими параметрами или
        недоступный Qdrant не дают воркеру стартовать (§10.1).
        """
        await self._index.ensure_collection()
        # Временные каталоги заданий остались от прежнего процесса (§1.6).
        await self._media.clean(uploads=False, scratch=True)
        requeued = await self._requeue_abandoned()
        logger.info(
            "worker started",
            extra={
                "concurrency": self._settings.concurrency,
                "chat_slots": self._settings.chat_slots,
                "requeued": requeued,
            },
        )
        loop = asyncio.get_running_loop()
        next_sweep = loop.time()
        running: set[asyncio.Task[None]] = set()
        transcribing: set[asyncio.Task[None]] = set()
        stopping = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                self._settings.heartbeat_file.touch()
                if loop.time() >= next_sweep:
                    await self._janitor.sweep()
                    next_sweep = loop.time() + _SWEEP_INTERVAL_SECONDS
                await self._fill(running, self._settings.concurrency, self._claim, self._run)
                await self._fill(
                    transcribing,
                    self._settings.chat_slots,
                    self._claim_transcription,
                    self._run_transcription,
                )
                await asyncio.wait(
                    {stopping, *running, *transcribing},
                    timeout=self._settings.poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                running = {task for task in running if not task.done()}
                transcribing = {task for task in transcribing if not task.done()}
        finally:
            stopping.cancel()
            for task in (*running, *transcribing):
                task.cancel()
            await asyncio.gather(*running, *transcribing, return_exceptions=True)
            logger.info("worker stopped")

    async def _requeue_abandoned(self) -> int:
        """Выровнять подписи состояния после аварийной остановки (§11.1).

        Документ или вложение могли остаться «обрабатывается», хотя аренда их задания уже
        истекла: до нового взятия они честно «в очереди».
        """
        now = self._clock.now()
        async with self._uow_factory() as uow:
            requeued = await uow.documents.requeue_abandoned(now)
            attachment_ids = await uow.jobs.unleased_transcriptions(now)
        for attachment_id in attachment_ids:
            await self._transcripts.requeue(attachment_id)
        return requeued

    async def run_once(self) -> bool:
        """Взять и выполнить одно задание основного места; `False` — доступных заданий нет."""
        job = await self._claim()
        if job is None:
            return False
        await self._run(job)
        return True

    async def run_transcription_once(self) -> bool:
        """Взять и выполнить одно задание `transcribe`; `False` — доступных заданий нет."""
        job = await self._claim_transcription()
        if job is None:
            return False
        await self._run_transcription(job)
        return True

    async def _fill[J](
        self,
        running: set[asyncio.Task[None]],
        slots: int,
        claim: Callable[[], Awaitable[J | None]],
        run: Callable[[J], Coroutine[Any, Any, None]],
    ) -> None:
        """Занять свободные места набора заданиями; сбой базы цикл не роняет."""
        try:
            while len(running) < slots:
                job = await claim()
                if job is None:
                    return
                running.add(asyncio.create_task(run(job)))
        except Exception as error:
            logger.error("queue unavailable", extra=_failure(error))

    async def _claim(self) -> Job | None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            job = await uow.jobs.claim(now, now + self._lease, self._processing)
        if job is not None and job.kind in _PROCESSING_KINDS:
            self._processing.add(job.document_id)
        return job

    async def _claim_transcription(self) -> TranscriptionJob | None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            return await uow.jobs.claim_transcription(now, now + self._lease)

    async def _run(self, job: Job) -> None:
        """Выполнить задание документа и записать его исход."""
        try:
            await self._leased(
                job.id, _job_fields(job), partial(self._execute, job), partial(self._release, job)
            )
        finally:
            if job.kind in _PROCESSING_KINDS:
                self._processing.discard(job.document_id)

    async def _run_transcription(self, job: TranscriptionJob) -> None:
        """Выполнить задание `transcribe`: исход во вложение записывает сам сценарий."""
        fields = {"job_id": str(job.id), "kind": "transcribe", "attempt": job.attempts}
        await self._leased(
            job.id,
            fields,
            lambda _: self._attachments.run(job),
            partial(self._release_transcription, job),
        )

    async def _leased(
        self,
        job_id: UUID,
        fields: dict[str, object],
        execute: Callable[[Callable[[], object]], Awaitable[None]],
        release: Callable[[], Awaitable[None]],
    ) -> None:
        """Выполнить взятое задание, продлевая аренду; исключения наружу не выходят.

        `execute` получает функцию «продление больше не нужно»: её вызывает задание,
        которое зафиксировало завершение и ещё убирает за собой, — иначе продление не
        нашло бы строки и приняло выполненное задание за снятое.
        """
        current = asyncio.current_task()
        assert current is not None
        renewal = asyncio.create_task(self._keep_lease(job_id, fields, current))
        try:
            await execute(renewal.cancel)
        except asyncio.CancelledError:
            if job_id in self._withdrawn:
                self._withdrawn.discard(job_id)
                logger.info("job withdrawn", extra=fields)
                return
            try:
                await asyncio.shield(release())
            except Exception as error:
                logger.error("job not released", extra={**fields, **_failure(error)})
            raise
        except Exception as error:
            # Исход записать не удалось (например, недоступна база): задание останется в
            # аренде и будет взято заново, когда она истечёт.
            logger.error("job outcome not saved", extra={**fields, **_failure(error)})
        finally:
            renewal.cancel()

    async def _execute(self, job: Job, settled: Callable[[], object]) -> None:
        if job.kind != "delete" and job.attempts > self._settings.max_attempts:
            # Предел достигнут ещё до взятия: на задании погибал сам процесс воркера (§11.1).
            logger.warning("job attempts exhausted", extra=_job_fields(job))
            if job.kind in _PROCESSING_KINDS:
                await self._indexer.prune(job.document_id)
            await self._give_up(job, "internal_error")
            return
        try:
            if job.kind == "delete":
                await self._indexer.erase(job)
            elif job.kind == "context":
                await self._contextualize(job)
            elif job.kind == "reprocess":
                await self._indexer.reprocess(job, settled)
            else:
                await self._indexer.index(job, settled)
        except JobAbandonedError:
            await self._finish(job)
            logger.info("job abandoned", extra=_job_fields(job))
        except PermanentIndexError as error:
            await self._give_up(job, error.code)
            logger.info("document rejected", extra={**_job_fields(job), "error_code": error.code})
        except Exception as error:
            logger.warning("job failed", extra={**_job_fields(job), **_failure(error)})
            await self._retry_later(job, _exhausted_code(error))
        else:
            logger.info("job done", extra=_job_fields(job))

    async def _contextualize(self, job: Job) -> None:
        """Одна порция пояснений; затем задание возвращает место воркера (§11.5).

        Выполненное задание снимает сам этап — в той же транзакции, где он убедился, что
        фрагментов без пояснения нет.
        """
        if await self._contextualizer.run(job):
            async with self._uow_factory() as uow:
                await uow.jobs.requeue(job.id)

    async def _keep_lease(
        self, job_id: UUID, fields: dict[str, object], task: asyncio.Task[None]
    ) -> None:
        """Продлевать аренду; если задания больше нет — прекратить его работу."""
        while True:
            await asyncio.sleep(self._settings.lease_seconds / _RENEWALS_PER_LEASE)
            try:
                async with self._uow_factory() as uow:
                    kept = await uow.jobs.renew(job_id, self._clock.now() + self._lease)
            except Exception as error:
                logger.error("lease not renewed", extra={**fields, **_failure(error)})
                continue
            if not kept:
                self._withdrawn.add(job_id)
                task.cancel()
                return

    async def _retry_later(self, job: Job, exhausted_code: ErrorCode) -> None:
        """Временный сбой: отложить задание с нарастающей паузой либо исчерпать попытки.

        Задание `delete` повторяется без ограничения: документ уже невидим, и стереть
        его нужно обязательно (§11.3).
        """
        settings = self._settings
        if job.kind != "delete" and job.attempts >= settings.max_attempts:
            await self._give_up(job, exhausted_code)
            return
        pause = min(
            settings.retry_base_seconds * 2 ** (job.attempts - 1), settings.retry_max_seconds
        )
        async with self._uow_factory() as uow:
            if job.kind == "index" and await uow.documents.lock(job.document_id) is not None:
                await uow.documents.set_status(job.document_id, "queued")
            await uow.jobs.postpone(job.id, self._clock.now() + timedelta(seconds=pause))

    async def _give_up(self, job: Job, code: ErrorCode) -> None:
        """Снять задание, которое не удалось: постоянный сбой или исчерпаны попытки.

        `index` — документ в состоянии ошибки; `reprocess` — документ остаётся готов, с
        прежним индексом, прежними картинками и отметкой о сбое (§11.6); `context` —
        оставшиеся фрагменты ищутся без пояснения (§11.5).
        """
        if job.kind == "reprocess":
            await self._indexer.drop_unindexed_images(job.document_id)
        async with self._uow_factory() as uow:
            if await uow.documents.lock(job.document_id) is not None:
                if job.kind == "index":
                    await uow.documents.set_status(job.document_id, "error", code)
                elif job.kind == "reprocess":
                    await uow.documents.set_reprocess_error(job.document_id, code)
            await uow.jobs.finish(job.id)

    async def _finish(self, job: Job) -> None:
        async with self._uow_factory() as uow:
            await uow.jobs.finish(job.id)

    async def _release(self, job: Job) -> None:
        """Штатная остановка (§11.1): задание сразу доступно, попытка не считается.

        Документ возвращается в `queued`: пока воркер остановлен, его никто не обрабатывает.
        """
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(job.document_id)
            # Задания уже нет — оно успело завершиться: документ готов, а не «в очереди».
            if await uow.jobs.release(job.id) and job.kind == "index" and document is not None:
                await uow.documents.set_status(job.document_id, "queued")

    async def _release_transcription(self, job: TranscriptionJob) -> None:
        """Штатная остановка: вложение снова «в очереди», попытка не считается (§11.1)."""
        await self._transcripts.requeue(job.attachment_id)
        async with self._uow_factory() as uow:
            await uow.jobs.release(job.id)


def _failure(error: Exception) -> dict[str, object]:
    """Сбой для журнала: тип и места в коде, без текста исключения."""
    return {"error_type": type(error).__name__, "trace": code_locations(error)}
