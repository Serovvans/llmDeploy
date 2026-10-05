"""Цикл очереди заданий базы знаний: аренда, продление, повторы (docs/portal-api.md §11).

Задание идемпотентно и переживает перезапуск: брошенное берётся заново по истечении
аренды, а при штатной остановке возвращается в очередь сразу.
"""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from portal.core.logging import code_locations
from portal.core.ports import Clock
from portal.core.settings import KbWorkerSettings
from portal.kb.domain import ErrorCode, Job
from portal.kb.indexing import Indexer, JobAbandonedError, PermanentIndexError
from portal.kb.ports import RecognitionFailedError, VectorIndex
from portal.kb.store import KbUnitOfWorkFactory

logger = logging.getLogger(__name__)

# Аренда продлевается не реже раза в треть её срока (§11.1).
_RENEWALS_PER_LEASE = 3


def _job_fields(job: Job) -> dict[str, object]:
    """Поля задания для журнала: только идентификаторы и счётчики."""
    return {
        "job_id": str(job.id),
        "document_id": str(job.document_id),
        "kind": job.kind,
        "attempt": job.attempts,
    }


class Worker:
    """Берёт задания из `kb_jobs` и выполняет не больше `concurrency` одновременно."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        indexer: Indexer,
        index: VectorIndex,
        clock: Clock,
        settings: KbWorkerSettings,
    ) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._indexer = indexer
        self._index = index
        self._clock = clock
        self._settings = settings
        self._lease = timedelta(seconds=settings.lease_seconds)
        # Задания, которые сняты, пока выполнялись: их аренду возвращать незачем.
        self._withdrawn: set[UUID] = set()

    async def run(self, stop: asyncio.Event) -> None:
        """Работать, пока не выставлен `stop`; затем вернуть взятые задания в очередь.

        Перед началом создаётся коллекция Qdrant; коллекция с чужими параметрами или
        недоступный Qdrant не дают воркеру стартовать (§10.1).
        """
        await self._index.ensure_collection()
        # После падения воркера документ мог остаться «обрабатывается», хотя аренда его
        # задания уже истекла: до нового взятия он честно «в очереди».
        async with self._uow_factory() as uow:
            requeued = await uow.documents.requeue_abandoned(self._clock.now())
        logger.info(
            "worker started",
            extra={"concurrency": self._settings.concurrency, "requeued": requeued},
        )
        running: set[asyncio.Task[None]] = set()
        stopping = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                self._settings.heartbeat_file.touch()
                await self._fill(running)
                await asyncio.wait(
                    {stopping, *running},
                    timeout=self._settings.poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                running = {task for task in running if not task.done()}
        finally:
            stopping.cancel()
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            logger.info("worker stopped")

    async def run_once(self) -> bool:
        """Взять и выполнить одно задание; `False` — доступных заданий нет."""
        job = await self._claim()
        if job is None:
            return False
        await self._run(job)
        return True

    async def _fill(self, running: set[asyncio.Task[None]]) -> None:
        """Занять свободные места заданиями; сбой базы цикл не роняет."""
        try:
            while len(running) < self._settings.concurrency:
                job = await self._claim()
                if job is None:
                    return
                running.add(asyncio.create_task(self._run(job)))
        except Exception as error:
            logger.error("queue unavailable", extra=_failure(error))

    async def _claim(self) -> Job | None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            return await uow.jobs.claim(now, now + self._lease)

    async def _run(self, job: Job) -> None:
        """Выполнить задание и записать его исход; исключения наружу не выходят."""
        current = asyncio.current_task()
        assert current is not None
        renewal = asyncio.create_task(self._keep_lease(job, current))
        try:
            await self._execute(job)
        except asyncio.CancelledError:
            if job.id in self._withdrawn:
                self._withdrawn.discard(job.id)
                logger.info("job withdrawn", extra=_job_fields(job))
                return
            await asyncio.shield(self._release(job))
            raise
        except Exception as error:
            # Исход записать не удалось (например, недоступна база): задание останется в
            # аренде и будет взято заново, когда она истечёт.
            logger.error("job outcome not saved", extra={**_job_fields(job), **_failure(error)})
        finally:
            renewal.cancel()

    async def _execute(self, job: Job) -> None:
        try:
            if job.kind == "delete":
                await self._indexer.erase(job)
            else:
                await self._indexer.index(job)
        except JobAbandonedError:
            await self._finish(job)
            logger.info("job abandoned", extra=_job_fields(job))
        except PermanentIndexError as error:
            await self._fail(job, error.code)
            logger.info("document rejected", extra={**_job_fields(job), "error_code": error.code})
        except Exception as error:
            logger.warning("job failed", extra={**_job_fields(job), **_failure(error)})
            await self._retry_later(job, isinstance(error, RecognitionFailedError))
        else:
            logger.info("job done", extra=_job_fields(job))

    async def _keep_lease(self, job: Job, task: asyncio.Task[None]) -> None:
        """Продлевать аренду; если задания больше нет — прекратить его работу."""
        while True:
            await asyncio.sleep(self._settings.lease_seconds / _RENEWALS_PER_LEASE)
            try:
                async with self._uow_factory() as uow:
                    kept = await uow.jobs.renew(job.id, self._clock.now() + self._lease)
            except Exception as error:
                logger.error("lease not renewed", extra={**_job_fields(job), **_failure(error)})
                continue
            if not kept:
                self._withdrawn.add(job.id)
                task.cancel()
                return

    async def _retry_later(self, job: Job, recognition: bool) -> None:
        """Временный сбой: отложить задание с нарастающей паузой либо исчерпать попытки.

        Задание `delete` повторяется без ограничения: документ уже невидим, и стереть
        его нужно обязательно (§11.3).
        """
        settings = self._settings
        if job.kind == "index" and job.attempts >= settings.max_attempts:
            await self._fail(job, "recognition_failed" if recognition else "internal_error")
            return
        pause = min(
            settings.retry_base_seconds * 2 ** (job.attempts - 1), settings.retry_max_seconds
        )
        async with self._uow_factory() as uow:
            if job.kind == "index" and await uow.documents.lock(job.document_id) is not None:
                await uow.documents.set_status(job.document_id, "queued")
            await uow.jobs.postpone(job.id, self._clock.now() + timedelta(seconds=pause))

    async def _fail(self, job: Job, code: ErrorCode) -> None:
        """Перевести документ в состояние ошибки и снять задание."""
        async with self._uow_factory() as uow:
            if await uow.documents.lock(job.document_id) is not None:
                await uow.documents.set_status(job.document_id, "error", code)
            await uow.jobs.finish(job.id)

    async def _finish(self, job: Job) -> None:
        async with self._uow_factory() as uow:
            await uow.jobs.finish(job.id)

    async def _release(self, job: Job) -> None:
        """Штатная остановка (§11.1): задание сразу доступно, попытка не считается.

        Документ возвращается в `queued`: пока воркер остановлен, его никто не обрабатывает.
        """
        try:
            async with self._uow_factory() as uow:
                if job.kind == "index" and await uow.documents.lock(job.document_id) is not None:
                    await uow.documents.set_status(job.document_id, "queued")
                await uow.jobs.release(job.id)
        except Exception as error:
            logger.error("job not released", extra={**_job_fields(job), **_failure(error)})


def _failure(error: Exception) -> dict[str, object]:
    """Сбой для журнала: тип и места в коде, без текста исключения."""
    return {"error_type": type(error).__name__, "trace": code_locations(error)}
