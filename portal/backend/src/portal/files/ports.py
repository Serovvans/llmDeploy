"""Порты работы с файлами (docs/portal-api.md §13.3); ими пользуются dialogs, tools, kb."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

MediaType = Literal[
    "image/jpeg",
    "image/png",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
]
RecordingType = Literal[
    "audio/mpeg",
    "audio/wav",
    "audio/flac",
    "audio/ogg",
    "audio/mp4",
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
]
StorageArea = Literal["attachments", "docparse", "kb", "kb-images"]
QuotaReason = Literal["total", "user", "free_space"]
StallReason = Literal["idle", "total"]

DOCX: MediaType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class StoredFile:
    """Сохранённый файл; `key` — `<область>/<uuid>`."""

    key: str
    size_bytes: int
    sha256: bytes


@dataclass(frozen=True)
class DocumentImage:
    """Встроенная картинка документа, готовая к хранению (§8.6).

    Длинная сторона не больше `llm.image_max_side_px`; `width` и `height` — размер
    сохраняемой картинки; `sha256` — от исходных байтов картинки в документе.
    """

    data: bytes
    media_type: Literal["image/png", "image/jpeg"]
    width: int
    height: int
    sha256: bytes


@dataclass(frozen=True)
class StoredRecording:
    """Принятая запись (аудио или видео); `key` — `media/<owner_id>/<uuid>`."""

    key: str
    size_bytes: int
    sha256: bytes
    media_type: RecordingType


class FileTooLargeError(Exception):
    """Файл больше лимита (у записи — тело длиннее заявленного); приём оборван."""


class TooManyUploadsError(Exception):
    """Все места загрузки записей заняты либо у этого сотрудника уже идёт загрузка."""


class MediaQuotaExceededError(Exception):
    """Запись не помещается: квота портала, квота сотрудника или свободное место тома."""

    def __init__(self, reason: QuotaReason) -> None:
        """Запомнить, какой предел сработал."""
        super().__init__(reason)
        self.reason: QuotaReason = reason


class UnsupportedMediaError(Exception):
    """Первые байты тела — не запись разрешённого типа (§1.5)."""


class UploadStalledError(Exception):
    """Приём записи оборван по сроку: `idle` — нет новых байт, `total` — общий срок."""

    def __init__(self, reason: StallReason) -> None:
        """Запомнить, какой срок вышел."""
        super().__init__(reason)
        self.reason: StallReason = reason


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

    def embedded_images(
        self, path: Path, media_type: MediaType, page: int, *, min_side_px: int, min_area_px: int
    ) -> list[DocumentImage]:
        """Встроенные растровые картинки страницы по порядку (§8.6).

        У DOCX и файла-картинки `page = 1` — все картинки документа. Картинки ниже порогов
        и крупнее `files.image_max_pixels` пропускаются без декодирования. Страница-скан
        PDF и текстовый файл возвращают `[]`. Из DOCX — только части с сигнатурой JPEG или
        PNG. `UnreadableDocumentError`.
        """
        ...


class MediaStorage(Protocol):
    """Хранилище записей: приём потоком с квотой и резервом места (§1.6)."""

    async def receive(
        self, owner_id: UUID, chunks: AsyncIterator[bytes], declared_bytes: int
    ) -> StoredRecording:
        """Принять запись потоком в том; тип — по первым байтам.

        До чтения тела: число загрузок (`TooManyUploadsError`), квота и свободное место
        по заявленной длине (`MediaQuotaExceededError`), резерв. По ходу:
        `UnsupportedMediaError`, `FileTooLargeError` (тело длиннее заявленного),
        `UploadStalledError`. Любой отказ и отмена вызова удаляют файл и освобождают
        резерв и место загрузки.
        """
        ...

    def new_key(self, owner_id: UUID) -> str:
        """Ключ для сжатого звука, который пишет воркер."""
        ...

    def path(self, key: str) -> Path:
        """Путь к файлу записи на диске."""
        ...

    async def delete(self, key: str) -> None:
        """Удалить файл; отсутствие файла — не ошибка."""
        ...

    def scratch(self, job_id: UUID) -> Path:
        """Пустой временный каталог задания `media/tmp/<job_id>/`."""
        ...

    async def drop_scratch(self, job_id: UUID) -> None:
        """Удалить временный каталог задания; отсутствие — не ошибка."""
        ...

    async def clean(self, *, uploads: bool, scratch: bool) -> None:
        """Уборка при старте: недописанные загрузки `*.part` и (или) каталог `media/tmp/`."""
        ...
