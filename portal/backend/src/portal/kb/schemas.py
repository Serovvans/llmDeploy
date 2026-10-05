"""Тела ответов маршрутов базы знаний (docs/portal-api.md §8.1, §8.3)."""

from uuid import UUID

from pydantic import BaseModel

from portal.core.ports import CurrentUser
from portal.core.schemas import ApiTime
from portal.kb.domain import DocumentStatus, DocumentView, ErrorCode, Scope, can_delete
from portal.kb.service import DocumentText


class AuthorOut(BaseModel):
    """Автор документа."""

    full_name: str
    is_me: bool


class ProgressOut(BaseModel):
    """Ход обработки по страницам."""

    pages_done: int
    pages_total: int
    recognizing: bool


class KbDocumentOut(BaseModel):
    """Объект `KbDocument`; тип и размер файла интерфейсу не отдаются (§8.2)."""

    id: UUID
    title: str
    scope: Scope
    is_cogis: bool
    author: AuthorOut
    created_at: ApiTime
    page_count: int | None
    status: DocumentStatus
    error_code: ErrorCode | None
    progress: ProgressOut | None
    can_delete: bool

    @classmethod
    def of(cls, view: DocumentView, user: CurrentUser) -> "KbDocumentOut":
        """Собрать из документа глазами текущего пользователя."""
        document = view.document
        progress = None
        if document.status == "processing" and document.page_count is not None:
            progress = ProgressOut(
                pages_done=document.pages_done,
                pages_total=document.page_count,
                recognizing=document.recognizing,
            )
        return cls(
            id=document.id,
            title=document.title,
            scope=document.scope,
            is_cogis=document.is_cogis,
            author=AuthorOut(full_name=view.author_full_name, is_me=document.owner_id == user.id),
            created_at=document.created_at,
            page_count=document.page_count,
            status=document.status,
            error_code=document.error_code if document.status == "error" else None,
            progress=progress,
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
    """Отрезок текста страницы."""

    text: str
    highlight: bool


class DocumentTextOut(BaseModel):
    """Объект `DocumentText`."""

    page: int | None
    page_count: int | None
    recognized: bool
    segments: list[SegmentOut]

    @classmethod
    def of(cls, text: DocumentText) -> "DocumentTextOut":
        """Собрать из текста страницы."""
        return cls(
            page=text.page,
            page_count=text.page_count,
            recognized=text.recognized,
            segments=[
                SegmentOut(text=segment.text, highlight=segment.highlight)
                for segment in text.segments
            ],
        )


class CogisDocumentationOut(BaseModel):
    """Есть ли в общей базе готовая документация CoGIS."""

    available: bool
