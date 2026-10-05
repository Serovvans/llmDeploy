"""Хранилище файлов на диске: имена генерирует сервер (docs/portal-api.md §1.5)."""

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from portal.files.ports import FileTooLargeError, StorageArea, StoredFile

_KEY_PATTERN = re.compile(r"^(attachments|docparse|kb)/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class DiskFileStorage:
    """Реализация порта `FileStorage`: `<корень>/<область>/<uuid>`."""

    def __init__(self, root: Path) -> None:
        """Запомнить корневой каталог (volume `portal-files`)."""
        self._root = root

    async def save(
        self, area: StorageArea, chunks: AsyncIterator[bytes], max_bytes: int
    ) -> StoredFile:
        """Принять файл потоком; на превышении лимита приём обрывается, файл удаляется."""
        key = f"{area}/{uuid4()}"
        path = self.path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("wb") as file:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError
                    digest.update(chunk)
                    await asyncio.to_thread(file.write, chunk)
        except BaseException:
            await self.delete(key)
            raise
        return StoredFile(key=key, size_bytes=size, sha256=digest.digest())

    def path(self, key: str) -> Path:
        """Путь к файлу; ключ иного вида, чем выдаёт `save`, отвергается."""
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("недопустимый ключ файла")
        return self._root / key

    async def delete(self, key: str) -> None:
        """Удалить файл; отсутствие файла — не ошибка."""
        await asyncio.to_thread(self.path(key).unlink, missing_ok=True)
