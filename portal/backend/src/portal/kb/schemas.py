"""Тела запросов и ответов маршрутов базы знаний (docs/portal-api.md §8.1, §8.3, §8.5)."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from portal.core.ports import CurrentUser
from portal.core.schemas import ApiTime, RequestModel
from portal.kb.domain import DocumentStatus, DocumentView, ErrorCode, Scope, can_delete
from portal.kb.service import DocumentText


class TagsIn(RequestModel):
    """Тело правки тегов: список заменяет прежний целиком; правила тега проверяет сценарий."""

    tags: list[str]


class TagsOut(BaseModel):
    """Подсказки тегов."""

    items: list[str]


class AuthorOut(BaseModel):
    """Автор документа."""

    full_name: str
    is_me: bool


class ProgressOut(BaseModel):
    """Ход обработки по страницам."""

    pages_done: int
    pages_total: int
    recognizing: bool


class RecordingProgressOut(BaseModel):
    """Ход расшифровки записи по секундам."""

    seconds_done: int
    seconds_total: int


class KbDocumentOut(BaseModel):
    """Объект `KbDocument`; тип и размер файла интерфейсу не отдаются (§8.2)."""

    id: UUID
    title: str
    scope: Scope
    kind: Literal["document", "recording"]
    is_cogis: bool
    tags: list[str]
    author: AuthorOut
    created_at: ApiTime
    page_count: int | None
    duration_seconds: int | None
    status: DocumentStatus
    error_code: ErrorCode | None
    progress: ProgressOut | RecordingProgressOut | None
    images_truncated: bool
    reprocess_error: ErrorCode | None
    reprocessing: bool
    can_delete: bool

    @classmethod
    def of(cls, view: DocumentView, user: CurrentUser) -> "KbDocumentOut":
        """Собрать из документа глазами текущего пользователя."""
        document = view.document
        progress: ProgressOut | RecordingProgressOut | None = None
        if document.status != "processing":
            pass
        elif document.page_count is not None:
            progress = ProgressOut(
                pages_done=document.pages_done,
                pages_total=document.page_count,
                recognizing=document.recognizing,
            )
        elif document.is_recording and document.duration_seconds is not None:
            progress = RecordingProgressOut(
                seconds_done=document.seconds_done, seconds_total=document.duration_seconds
            )
        return cls(
            id=document.id,
            title=document.title,
            scope=document.scope,
            kind="recording" if document.is_recording else "document",
            is_cogis=document.is_cogis,
            tags=list(view.tags),
            author=AuthorOut(full_name=view.author_full_name, is_me=document.owner_id == user.id),
            created_at=document.created_at,
            page_count=document.page_count,
            duration_seconds=document.duration_seconds,
            status=document.status,
            error_code=document.error_code if document.status == "error" else None,
            progress=progress,
            images_truncated=document.images_truncated,
            reprocess_error=document.reprocess_error,
            reprocessing=view.reprocessing,
            can_delete=can_delete(user, document),
        )


class KbDocumentPageOut(BaseModel):
    """Страница списка документов."""

    items: list[KbDocumentOut]
    page: int
    page_size: int
    total: int


class DuplicateOut(BaseModel):
    """`details.document` отказа `duplicate_document`."""

    id: UUID
    title: str
    author_full_name: str
    created_at: ApiTime
    status: DocumentStatus
    error_code: ErrorCode | None
    can_delete: bool

    @classmethod
    def of(cls, view: DocumentView, user: CurrentUser) -> "DuplicateOut":
        """Собрать из уже существующего документа."""
        document = view.document
        return cls(
            id=document.id,
            title=document.title,
            author_full_name=view.author_full_name,
            created_at=document.created_at,
            status=document.status,
            error_code=document.error_code if document.status == "error" else None,
            can_delete=can_delete(user, document),
        )


class SegmentOut(BaseModel):
    """Отрезок текста страницы; `start_seconds` — время начала отрезка речи у записи."""

    text: str
    highlight: bool
    start_seconds: int | None


class ImageOut(BaseModel):
    """Картинка источника-картинки и её описание (§8.3)."""

    id: UUID
    description: str


class DocumentTextOut(BaseModel):
    """Объект `DocumentText`."""

    page: int | None
    page_count: int | None
    recognized: bool
    segments: list[SegmentOut]
    timecode_seconds: int | None
    image: ImageOut | None

    @classmethod
    def of(cls, text: DocumentText) -> "DocumentTextOut":
        """Собрать из текста страницы."""
        return cls(
            page=text.page,
            page_count=text.page_count,
            recognized=text.recognized,
            segments=[
                SegmentOut(
                    text=segment.text,
                    highlight=segment.highlight,
                    start_seconds=segment.start_seconds,
                )
                for segment in text.segments
            ],
            timecode_seconds=text.timecode_seconds,
            image=None
            if text.image is None
            else ImageOut(id=text.image.id, description=text.image.description or ""),
        )


class CogisDocumentationOut(BaseModel):
    """Есть ли в общей базе готовая документация CoGIS."""

    available: bool
