"""Возврат документов в очередь командой `portal reindex` (docs/portal-api.md §13.4)."""

from uuid import uuid4

from portal.core.ports import Clock
from portal.kb.domain import Job
from portal.kb.ports import VectorIndex
from portal.kb.store import KbUnitOfWorkFactory


class WorkerRunningError(Exception):
    """Есть задания в аренде: пересоздавать коллекцию при работающем воркере нельзя."""


class Reindexer:
    """Ставит документы в очередь заново; уже прочитанные страницы сохраняются."""

    def __init__(self, uow_factory: KbUnitOfWorkFactory, index: VectorIndex, clock: Clock) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._index = index
        self._clock = clock

    async def run(self, *, only_errors: bool, recreate_collection: bool) -> int:
        """Вернуть в очередь неудалённые документы; результат — сколько их затронуто.

        `only_errors` — только документы в состоянии `error`. `recreate_collection` —
        сначала удалить и создать заново коллекцию Qdrant (смена модели эмбеддингов).
        """
        if recreate_collection:
            async with self._uow_factory() as uow:
                if await uow.jobs.has_leased(self._clock.now()):
                    raise WorkerRunningError
            await self._index.drop_collection()
            await self._index.ensure_collection()
        async with self._uow_factory() as uow:
            document_ids = await uow.documents.ids_for_reindex(only_errors=only_errors)
        requeued = 0
        for document_id in document_ids:
            now = self._clock.now()
            async with self._uow_factory() as uow:
                document = await uow.documents.lock(document_id)
                # Пока шёл обход, документ могли удалить или обработать повторно.
                if document is None or (only_errors and document.status != "error"):
                    continue
                await uow.documents.set_status(document_id, "queued")
                await uow.jobs.reset_or_add(Job(uuid4(), document_id, "index", 0, now, None, now))
            requeued += 1
        return requeued
