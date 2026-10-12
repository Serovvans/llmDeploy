"""Сущности базы знаний и права на них (docs/portal-api.md §8.1, §9.11–9.14)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from portal.core.ports import CurrentUser
from portal.files.ports import MediaType, RecordingType
from portal.kb.ports import Collection

Scope = Collection
DocumentStatus = Literal["queued", "processing", "ready", "error"]
ErrorCode = Literal[
    "file_unreadable",
    "no_text",
    "document_too_long",
    "recognition_failed",
    "internal_error",
    # Только у записей (§8.1):
    "media_invalid",
    "media_no_audio",
    "media_too_long",
    "transcription_failed",
    "source_expired",
]
JobKind = Literal["index", "delete", "context", "reprocess"]
SortField = Literal["created_at", "title"]

# Порядок обслуживания заданий: меньше — раньше (§11). У `reprocess` и `context` здесь
# приоритет по умолчанию; другой задаётся явно.
JOB_PRIORITY: dict[JobKind, int] = {"delete": 0, "index": 20, "context": 30, "reprocess": 40}
# С этого приоритета задание относится к массовой переобработке: только их касается пауза,
# а `reprocess` с меньшим приоритетом поставлен по запросу пользователя (§8.1, §11).
BULK_PRIORITY_FROM = 35
# Пояснения после массовой переобработки: раньше следующего `reprocess`, позже новой загрузки.
BULK_CONTEXT_PRIORITY = 35
# Порядок обработки, которым строят индекс задания `index` и `reprocess` (§9.11).
INDEX_VERSION = 2
# Расшифровка записи во вложении чата (§11.7): раньше заданий индексации.
TRANSCRIBE_PRIORITY = 10
# Сколько раз берётся задание `transcribe`: одна повторная попытка сразу, без паузы.
TRANSCRIBE_MAX_ATTEMPTS = 2
_RECORDING_TYPES = ("audio/", "video/")


@dataclass(frozen=True)
class KbDocument:
    """Документ базы знаний; `deleted_at` заполнено — ждёт стирания воркером."""

    id: UUID
    owner_id: UUID
    scope: Scope
    is_cogis: bool
    title: str
    media_type: MediaType | RecordingType
    size_bytes: int
    sha256: bytes
    # `None` — только у записи, исходный файл которой удалён (§8.7).
    storage_key: str | None
    page_count: int | None
    status: DocumentStatus
    error_code: ErrorCode | None
    pages_done: int
    recognizing: bool
    created_at: datetime
    deleted_at: datetime | None
    duration_seconds: int | None = None
    seconds_done: int = 0
    audio_key: str | None = None
    images_truncated: bool = False
    index_version: int = 1
    reprocess_error: ErrorCode | None = None

    @property
    def is_recording(self) -> bool:
        """Запись (аудио или видео): расшифровывается, а не читается по страницам (§8.7)."""
        return self.media_type.startswith(_RECORDING_TYPES)

    @property
    def has_nothing_to_process(self) -> bool:
        """Записи нечем обработаться: нет ни исходника, ни сжатого звука (§8.7)."""
        return self.is_recording and self.storage_key is None and self.audio_key is None


@dataclass(frozen=True)
class DocumentView:
    """Документ глазами пользователя: с ФИО автора и тегами — так он показывается в списках.

    `tags` — написания тегов по алфавиту ключей. `reprocessing` — документ готов и
    переобрабатывается по запросу пользователя (§8.1).
    """

    document: KbDocument
    author_full_name: str
    tags: tuple[str, ...] = ()
    reprocessing: bool = False


@dataclass(frozen=True)
class Job:
    """Задание очереди: строка есть, пока работа не выполнена."""

    id: UUID
    document_id: UUID
    kind: JobKind
    priority: int
    attempts: int
    run_after: datetime
    locked_until: datetime | None
    created_at: datetime
    attachment_id: UUID | None = None

    @classmethod
    def new(
        cls, document_id: UUID, kind: JobKind, now: datetime, priority: int | None = None
    ) -> "Job":
        """Новое задание документа, доступное сразу; без `priority` — приоритет своего вида."""
        return cls(
            uuid4(),
            document_id,
            kind,
            JOB_PRIORITY[kind] if priority is None else priority,
            0,
            now,
            None,
            now,
        )


@dataclass(frozen=True)
class TranscriptionJob:
    """Взятое задание `transcribe`: расшифровка записи во вложении чата (§11.7)."""

    id: UUID
    attachment_id: UUID
    attempts: int


@dataclass(frozen=True)
class ReprocessStatus:
    """Числа для `portal reprocess status` (§11.6): без названий и содержимого документов."""

    current: int  # готовых документов с текущим `index_version`
    reprocess_jobs: int
    context_jobs: int
    errors: dict[str, int]  # код причины → сколько документов с `reprocess_error`
    paused: bool


@dataclass(frozen=True)
class PageText:
    """Текст страницы; у документа без страниц — единственная страница с номером 1.

    У записи страница — часть расшифровки: `start_ms` — начало отрезка нарезки, `marks` —
    пары «смещение в тексте, время начала отрезка речи в мс» (§8.7).
    """

    number: int
    text: str
    recognized: bool
    start_ms: int | None = None
    marks: Sequence[Sequence[int]] | None = None


@dataclass(frozen=True)
class KbImage:
    """Картинка документа (§8.6, §9.18); `description is None` — ещё не описана."""

    id: UUID
    document_id: UUID
    page_number: int | None
    ordinal: int
    sha256: bytes
    storage_key: str
    media_type: Literal["image/png", "image/jpeg"]
    width: int
    height: int
    description: str | None = None


@dataclass(frozen=True)
class Fragment:
    """Фрагмент: отрезок текста одной страницы, в символах; он же точка в Qdrant."""

    id: UUID
    document_id: UUID
    page_number: int
    ordinal: int
    start_offset: int
    end_offset: int
    section: str | None = None
    context: str | None = None
    image_id: UUID | None = None
    start_ms: int | None = None


@dataclass(frozen=True)
class FragmentText:
    """Фрагмент с текстом глазами воркера: для пояснений (§11.5)."""

    id: UUID
    ordinal: int
    section: str | None
    text: str


@dataclass(frozen=True)
class FoundFragment:
    """Найденный фрагмент, прошедший повторную проверку доступа в базе.

    `page` — страница для источника (`None` у документа без страниц); `page_number` и
    границы — место в тексте страницы: по ним склеиваются соседние фрагменты. `section`
    и `context` служат только поиску и дальше него не идут (§8.4). У фрагмента-картинки
    `text` — её описание, у фрагмента расшифровки заполнен `start_ms`.
    """

    fragment_id: UUID
    document_id: UUID
    document_title: str
    scope: Scope
    page: int | None
    text: str
    ordinal: int
    page_number: int
    start_offset: int
    end_offset: int
    section: str | None
    context: str | None
    image_id: UUID | None = None
    start_ms: int | None = None


def can_add(user: CurrentUser, scope: Scope) -> bool:
    """Право добавить документ в коллекцию (§8.1): «Министерства» ведёт только администратор."""
    return scope != "ministries" or user.role == "admin"


def can_delete(user: CurrentUser, document: KbDocument) -> bool:
    """Право распоряжаться документом: удалить, обработать повторно, сменить теги (§8.1).

    Решает роль на момент запроса. «Министерства» — только администратор, в том числе не
    автор; бывший администратор теряет право и на документы, которые добавил сам. Общая
    база — автор либо администратор; личная — только владелец.
    """
    if document.scope == "ministries":
        return user.role == "admin"
    return document.owner_id == user.id or (user.role == "admin" and document.scope == "shared")
