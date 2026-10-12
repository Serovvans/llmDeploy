"""Файлы вложений: документы лежат в хранилище файлов, записи — в хранилище записей."""

from portal.dialogs.domain import Attachment
from portal.files.ports import FileStorage, MediaStorage


class AttachmentFiles:
    """Удаляет файл вложения из того хранилища, в котором он лежит."""

    def __init__(self, storage: FileStorage, media: MediaStorage) -> None:
        """Запомнить оба хранилища."""
        self._storage = storage
        self._media = media

    async def delete(self, attachment: Attachment) -> None:
        """Удалить файл вложения; отсутствие файла — не ошибка (исходник записи уже удалён)."""
        store = self._media if attachment.is_recording else self._storage
        await store.delete(attachment.storage_key)
