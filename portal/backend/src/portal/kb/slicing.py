"""Квант задания и точки уступки основного места (docs/portal-api.md §11.1).

Задание `index` или `reprocess` после каждой пачки распознавания и порции описания
картинок проверяет, не пора ли отдать место. Квант отсчитывается от начала первой пачки
или порции во взятии: работа без точек уступки перед ней в квант не входит.
"""

from collections.abc import Collection
from datetime import datetime, timedelta
from uuid import UUID

from portal.core.ports import Clock
from portal.kb.domain import BULK_PRIORITY_FROM, Job
from portal.kb.outcomes import JobYieldedError
from portal.kb.store import KbUnitOfWorkFactory


class JobSlice:
    """Квант одного взятия задания обработки документа."""

    def __init__(
        self,
        job: Job,
        uow_factory: KbUnitOfWorkFactory,
        clock: Clock,
        slice_seconds: float,
        busy_documents: Collection[UUID],
    ) -> None:
        """Получить зависимости явно.

        `busy_documents` — документы, которые воркер сейчас обрабатывает: их задания
        `index` и `reprocess` место не взяли бы, поэтому и уступать им незачем.
        """
        self._job = job
        self._uow_factory = uow_factory
        self._clock = clock
        self._length = timedelta(seconds=slice_seconds)
        self._busy = busy_documents
        self._started: datetime | None = None

    @property
    def bulk(self) -> bool:
        """Задание массовой переобработки: его держит пауза (§11.6)."""
        return self._job.priority >= BULK_PRIORITY_FROM

    def start(self) -> None:
        """Начало пачки распознавания или порции описания: первое начинает квант."""
        if self._started is None:
            self._started = self._clock.now()

    async def check(self) -> None:
        """Точка уступки: пора отдать место — `JobYieldedError`.

        Пора, если массовая переобработка на паузе (без отсчёта времени) либо квант
        истёк и ждёт задание, которое основное место взяло бы сейчас: у обычного — не
        ниже своего приоритета, у массового — обычное.
        """
        if self._started is None:
            return
        now = self._clock.now()
        up_to = BULK_PRIORITY_FROM - 1 if self.bulk else self._job.priority
        async with self._uow_factory() as uow:
            if self.bulk and await uow.jobs.bulk_paused():
                raise JobYieldedError
            if now - self._started >= self._length and await uow.jobs.has_waiting(
                now, self._busy, up_to
            ):
                raise JobYieldedError
