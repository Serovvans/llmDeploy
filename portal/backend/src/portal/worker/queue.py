"""Порт `TranscriptionQueue` на SQLAlchemy Core: задания `transcribe` в `kb_jobs` (§11.7)."""

from collections.abc import Sequence
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from portal.core.ports import Clock
from portal.kb.domain import TRANSCRIBE_PRIORITY
from portal.kb.tables import kb_jobs


class SqlTranscriptionQueue:
    """Очередь расшифровки вложений чата; вызывает `portal/dialogs/`."""

    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        """Запомнить движок: каждый вызов — своя короткая транзакция."""
        self._engine = engine
        self._clock = clock

    async def enqueue(self, attachment_id: UUID) -> None:
        """Поставить задание расшифровки; повторный вызов второго задания не создаёт."""
        now = self._clock.now()
        async with self._engine.begin() as connection:
            await connection.execute(
                postgresql.insert(kb_jobs)
                .values(
                    id=uuid4(),
                    attachment_id=attachment_id,
                    kind="transcribe",
                    priority=TRANSCRIBE_PRIORITY,
                    run_after=now,
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[kb_jobs.c.attachment_id],
                    index_where=kb_jobs.c.attachment_id.is_not(None),
                )
            )

    async def pending(self, attachment_ids: Sequence[UUID]) -> set[UUID]:
        """У каких из этих вложений задание в очереди есть."""
        if not attachment_ids:
            return set()
        async with self._engine.connect() as connection:
            rows = await connection.scalars(
                sa.select(kb_jobs.c.attachment_id).where(
                    kb_jobs.c.attachment_id.in_(attachment_ids)
                )
            )
            return set(rows.all())
