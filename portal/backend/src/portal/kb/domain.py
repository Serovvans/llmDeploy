"""Сущности базы знаний (docs/portal-api.md §8.1, §9.11–9.14)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from portal.core.ports import CurrentUser
from portal.files.ports import MediaType

Scope = Literal["shared", "personal"]
DocumentStatus = Literal["queued", "processing", "ready", "error"]
ErrorCode = Literal[
    "file_unreadable", "no_text", "document_too_long", "recognition_failed", "internal_error"
]
JobKind = Literal["index", "delete"]
SortField = Literal["created_at", "title"]


@dataclass(frozen=True)
class KbDocument:
    """Документ базы знаний; `deleted_at` заполнено — ждёт стирания воркером."""

    id: UUID
    owner_id: UUID
    scope: Scope
    is_cogis: bool
    title: str
    media_type: MediaType
    size_bytes: int
    sha256: bytes
    storage_key: str
    page_count: int | None
    status: DocumentStatus
    error_code: ErrorCode | None
    pages_done: int
    recognizing: bool
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True)
class DocumentView:
    """Документ вместе с ФИО автора — так он показывается в списках."""

    document: KbDocument
    author_full_name: str


@dataclass(frozen=True)
class Job:
    """Задание очереди: строка есть, пока работа не выполнена."""

    id: UUID
    document_id: UUID
    kind: JobKind
    attempts: int
    run_after: datetime
    locked_until: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class PageText:
    """Текст страницы; у документа без страниц — единственная страница с номером 1."""

    number: int
    text: str
    recognized: bool


@dataclass(frozen=True)
class Fragment:
    """Фрагмент: отрезок текста одной страницы, в символах; он же точка в Qdrant."""

    id: UUID
    document_id: UUID
    page_number: int
    ordinal: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class FoundFragment:
    """Найденный фрагмент, прошедший повторную проверку доступа в базе."""

    fragment_id: UUID
    document_id: UUID
    document_title: str
    scope: Scope
    page: int | None
    text: str


def can_delete(user: CurrentUser, document: KbDocument) -> bool:
    """Право удалить документ и отправить его на повторную обработку (§8.1).

    Автор, либо администратор — но только для общих документов.
    """
    return document.owner_id == user.id or (user.role == "admin" and document.scope == "shared")
