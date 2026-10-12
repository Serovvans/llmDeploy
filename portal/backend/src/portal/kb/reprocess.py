"""Массовая переобработка: команды `portal reprocess` (docs/portal-api.md §11.6, §13.4)."""

from portal.core.ports import Clock
from portal.kb.domain import Job, ReprocessStatus
from portal.kb.store import KbUnitOfWorkFactory


class Reprocessor:
    """Ставит задания `reprocess`, приостанавливает и возобновляет их обслуживание."""

    def __init__(self, uow_factory: KbUnitOfWorkFactory, clock: Clock) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._clock = clock

    async def start(self, *, all_ready: bool) -> int:
        """Поставить задания готовым документам; результат — скольким они поставлены.

        Без `all_ready` — документам с прежним порядком обработки или с отметкой о сбое.
        Повторный запуск безопасен: второе задание на документ не создаётся.
        """
        async with self._uow_factory() as uow:
            document_ids = await uow.documents.ids_for_reprocess(all_ready=all_ready)
        started = 0
        for document_id in document_ids:
            async with self._uow_factory() as uow:
                document = await uow.documents.lock(document_id)
                # Пока шёл обход, документ могли удалить или вернуть в очередь.
                if document is None or document.status != "ready":
                    continue
                job = Job.new(document_id, "reprocess", self._clock.now())
                started += await uow.jobs.add_if_absent(job)
        return started

    async def set_paused(self, paused: bool) -> None:
        """Приостановить или возобновить массовую переобработку; воркер не перезапускается."""
        async with self._uow_factory() as uow:
            await uow.jobs.set_bulk_paused(paused)

    async def status(self) -> ReprocessStatus:
        """Числа хода переобработки."""
        async with self._uow_factory() as uow:
            return await uow.documents.reprocess_status()
