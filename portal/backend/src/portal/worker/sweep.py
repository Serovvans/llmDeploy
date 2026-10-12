"""Уборка воркера: исходники записей и файлы записей без строки в базе (§1.6, §8.7)."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from portal.core.logging import code_locations
from portal.core.ports import Clock
from portal.core.settings import MediaSettings
from portal.dialogs.ports import AttachmentTranscripts
from portal.files.ports import MediaStorage
from portal.kb.store import KbUnitOfWorkFactory

logger = logging.getLogger(__name__)


class RecordingJanitor:
    """Удаляет то, что записи оставили на диске сверх нужного; под блокировкой строки документа."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        media: MediaStorage,
        transcripts: AttachmentTranscripts,
        stale_keys: Callable[[datetime], Sequence[str]],
        clock: Clock,
        settings: MediaSettings,
    ) -> None:
        """Получить зависимости явно.

        `stale_keys` — ключи файлов записей на диске, изменённых раньше заданного времени:
        порт `MediaStorage` перечислять файлы не умеет.
        """
        self._uow_factory = uow_factory
        self._media = media
        self._transcripts = transcripts
        self._stale_keys = stale_keys
        self._clock = clock
        self._ttl = timedelta(hours=settings.failed_source_ttl_hours)

    async def sweep(self) -> None:
        """Один проход уборки; сбой пишется в журнал и работу воркера не прерывает."""
        expired_before = self._clock.now() - self._ttl
        try:
            await self._drop_sources(expired_before)
            await self._drop_orphans(expired_before)
        except Exception as error:
            logger.error(
                "recordings sweep failed",
                extra={"error_type": type(error).__name__, "trace": code_locations(error)},
            )

    async def _drop_sources(self, expired_before: datetime) -> None:
        """Исходники готовых записей, не удалённые из-за сбоя, и истёкшие — записей с ошибкой."""
        async with self._uow_factory() as uow:
            document_ids = await uow.documents.recordings_to_clean(expired_before)
        for document_id in document_ids:
            async with self._uow_factory() as uow:
                document = await uow.documents.lock(document_id)
                # Пока шёл обход, запись могли удалить или вернуть в очередь.
                if document is None or document.storage_key is None:
                    continue
                expired = document.status == "error" and document.created_at < expired_before
                if document.status != "ready" and not expired:
                    continue
                await self._media.delete(document.storage_key)
                # Без сжатого звука расшифровка не доведена до конца: обработать нечем.
                unfinished = expired and document.audio_key is None
                await uow.documents.clear_source(
                    document_id, "source_expired" if unfinished else None
                )

    async def _drop_orphans(self, modified_before: datetime) -> None:
        """Файлы записей старше срока, на ключ которых не ссылается ни одна строка."""
        stale = await asyncio.to_thread(self._stale_keys, modified_before)
        if not stale:
            return
        async with self._uow_factory() as uow:
            known = await uow.documents.recording_keys()
        known |= await self._transcripts.source_keys()
        for key in stale:
            if key not in known:
                await self._media.delete(key)
