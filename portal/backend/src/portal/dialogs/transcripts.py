"""Порт `AttachmentTranscripts` на SQLAlchemy Core: вложение-запись глазами воркера (§5.8).

Запросы служебные, без условия владельца: воркер знает только идентификатор вложения из
своего задания; владельца порт не раскрывает и текст расшифровки обратно не отдаёт.
"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from portal.dialogs.ports import RecordingAttachment, TranscriptionError
from portal.dialogs.tables import attachments

_UNFINISHED = ("queued", "processing")


class SqlAttachmentTranscripts:
    """Состояние расшифровки в таблице `attachments`."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Запомнить движок: каждый вызов — своя короткая транзакция."""
        self._engine = engine

    async def _move(
        self, attachment_id: UUID, sources: tuple[str, ...], **values: Any
    ) -> sa.Row[Any] | None:
        """Сменить состояние вложения, если оно в одном из `sources`; вернуть его строку."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                sa.update(attachments)
                .where(
                    attachments.c.id == attachment_id,
                    attachments.c.transcription_status.in_(sources),
                )
                .values(**values)
                .returning(attachments.c.storage_key, attachments.c.media_type)
            )
            return result.first()

    async def begin(self, attachment_id: UUID) -> RecordingAttachment | None:
        """Вложение → `processing`; `None` — вложения нет или расшифровка уже завершена.

        Вложение в `processing` берётся снова: прежний воркер погиб, не вернув его в очередь.
        """
        row = await self._move(attachment_id, _UNFINISHED, transcription_status="processing")
        return None if row is None else RecordingAttachment(row.storage_key, row.media_type)

    async def complete(self, attachment_id: UUID, text: str, duration_seconds: int) -> None:
        """Сохранить расшифровку как текст вложения: вложение → `ready`."""
        await self._move(
            attachment_id,
            ("processing",),
            transcription_status="ready",
            text_content=text,
            duration_seconds=duration_seconds,
        )

    async def fail(
        self, attachment_id: UUID, code: TranscriptionError, duration_seconds: int | None
    ) -> None:
        """Вложение → `error` с причиной."""
        await self._move(
            attachment_id,
            _UNFINISHED,
            transcription_status="error",
            transcription_error=code,
            duration_seconds=duration_seconds,
        )

    async def requeue(self, attachment_id: UUID) -> None:
        """Вернуть вложение из `processing` в `queued`."""
        await self._move(attachment_id, ("processing",), transcription_status="queued")

    async def source_keys(self) -> set[str]:
        """Ключи хранения всех вложений-записей."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                sa.select(attachments.c.storage_key).where(
                    attachments.c.transcription_status.is_not(None)
                )
            )
            return {row.storage_key for row in rows}
