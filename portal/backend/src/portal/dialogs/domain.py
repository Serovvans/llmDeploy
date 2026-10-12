"""Сущности диалогов (docs/portal-api.md §5.1, §9.6–9.8)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from portal.files.ports import MediaType, RecordingType

DialogKind = Literal["chat", "sql", "cogis", "docparse"]
MessageRole = Literal["user", "assistant"]
MessageStatus = Literal["complete", "streaming", "stopped", "length_limit", "error"]
AnswerMode = Literal["fast", "thorough"]
TranscriptionStatus = Literal["queued", "processing", "ready", "error"]

TITLE_MAX_LENGTH = 200


@dataclass
class Dialog:
    """Диалог пользователя."""

    id: UUID
    owner_id: UUID
    kind: DialogKind
    title: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class Message:
    """Вопрос или ответ.

    `params` — параметры запроса у вопроса (в API не отдаются). `sources` — снимок
    источников, на которые в ответе есть сноска; `sources_found` — сколько фрагментов
    нашёл поиск; оба `None`, если поиск в базе знаний не выполнялся (§5.1). `sql_check` —
    проверка блоков `sql` ответа, `sql_dangers` — опасные операции в запросе из вопроса;
    оба только в диалоге `sql` (§7.1). `sql_run` — итог проверки блоков ответа на тестовых
    данных (§7.4); `None` — проверки не было или её итог не сохранён.
    """

    id: UUID
    dialog_id: UUID
    position: int
    role: MessageRole
    content: str
    status: MessageStatus
    error_code: str | None
    reasoning: str | None
    reasoning_seconds: int | None
    params: dict[str, Any]
    sources: list[dict[str, Any]] | None
    sources_found: int | None
    sql_check: dict[str, Any] | None
    sql_dangers: list[str] | None
    dropped_messages: int
    created_at: datetime
    sql_run: dict[str, Any] | None = None


@dataclass
class Attachment:
    """Вложение чата; `message_id = None` — ещё не отправлено.

    `transcription_status = None` — вложение не запись (аудио или видео); у записи
    `text_content` — её расшифровка (§5.8).
    """

    id: UUID
    dialog_id: UUID
    message_id: UUID | None
    file_name: str
    media_type: MediaType | RecordingType
    size_bytes: int
    storage_key: str
    page_count: int | None
    text_content: str | None
    image_pages: list[int]
    created_at: datetime
    duration_seconds: int | None = None
    transcription_status: TranscriptionStatus | None = None
    transcription_error: str | None = None

    @property
    def image_count(self) -> int:
        """Сколько изображений вложение добавит в запрос к модели."""
        return len(self.image_pages)

    @property
    def is_recording(self) -> bool:
        """Запись (аудио или видео): её файл лежит в хранилище записей."""
        return self.transcription_status is not None


DocparseStatus = Literal["processing", "ready"]
SummaryStatus = Literal["streaming", "complete", "stopped", "length_limit", "error"]


@dataclass
class Docparse:
    """Разбор документа — содержимое диалога вида `docparse` (§7.3, §9.9).

    `status = "processing"` — таблицы реквизитов ещё нет, диалог скрыт от всех маршрутов.
    `fields` — снимок на момент разбора: пары «название — значение», `None` — реквизита
    в документе нет.
    """

    dialog_id: UUID
    status: DocparseStatus
    summary_status: SummaryStatus
    template_id: str
    template_title: str
    free_form: bool
    file_name: str
    media_type: MediaType
    storage_key: str
    size_bytes: int
    page_count: int | None
    fields: list[dict[str, Any]]
    summary: str
    document_text: str
    created_at: datetime
