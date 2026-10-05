"""Порты работы с файлами (docs/portal-api.md §13.3); ими пользуются dialogs, tools, kb."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

MediaType = Literal[
    "image/jpeg",
    "image/png",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
]
StorageArea = Literal["attachments", "docparse", "kb"]

DOCX: MediaType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class StoredFile:
    """Сохранённый файл; `key` — `<область>/<uuid>`."""

    key: str
    size_bytes: int
    sha256: bytes


class FileTooLargeError(Exception):
    """Файл больше лимита; приём оборван."""


class UnreadableDocumentError(Exception):
    """Файл не открывается: повреждён, защищён паролем или превышает пределы распаковки."""


class FileStorage(Protocol):
    """Хранилище оригиналов; путь никогда не строится из имени пользователя."""

    async def save(
        self, area: StorageArea, chunks: AsyncIterator[bytes], max_bytes: int
    ) -> StoredFile:
        """Принять файл потоком; превышение `max_bytes` — `FileTooLargeError`."""
        ...

    def path(self, key: str) -> Path:
        """Путь к файлу на диске."""
        ...

    async def delete(self, key: str) -> None:
        """Удалить файл; отсутствие файла — не ошибка."""
        ...


class DocumentReader(Protocol):
    """Чтение документов; вызовы блокирующие — их место в пуле потоков."""

    def detect(self, path: Path, file_name: str) -> MediaType | None:
        """Тип по содержимому; `None` — не из разрешённых."""
        ...

    def page_count(self, path: Path, media_type: MediaType) -> int | None:
        """Число страниц; `None` — у документа нет страниц. `UnreadableDocumentError`."""
        ...

    def page_text(self, path: Path, media_type: MediaType, page: int) -> str | None:
        """Текст страницы; `None` — текстового слоя нет. Без страниц `page = 1` — весь текст."""
        ...

    def page_image(self, path: Path, media_type: MediaType, page: int) -> bytes:
        """Изображение страницы в PNG."""
        ...
