"""Тела запросов и ответов диалогов (docs/portal-api.md §5.1–5.3)."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from portal.core.schemas import ApiTime, RequestModel
from portal.dialogs.domain import (
    AnswerMode,
    Attachment,
    Dialog,
    DialogKind,
    Docparse,
    Knowledge,
    MessageRole,
    MessageStatus,
    SummaryStatus,
)
from portal.dialogs.service import MessageView
from portal.files.ports import MediaType


class CreateDialogRequest(RequestModel):
    """`POST /api/dialogs`; диалог `docparse` создаётся только запуском разбора."""

    kind: Literal["chat", "sql", "cogis"]


class RenameDialogRequest(RequestModel):
    """`PATCH /api/dialogs/{id}`."""

    title: str


class ChatMessageRequest(RequestModel):
    """Тело сообщения в диалоге `chat` (§5.3)."""

    content: str
    attachment_ids: tuple[UUID, ...] = ()
    mode: AnswerMode
    knowledge: Knowledge


class SqlMessageRequest(RequestModel):
    """Тело сообщения в диалоге `sql`: все поля обязательны, `schema_id: null` — без схемы."""

    content: str
    action: Literal["write", "explain", "debug", "optimize"]
    dialect: str
    schema_id: UUID | None


class CogisMessageRequest(RequestModel):
    """Тело сообщения в диалоге `cogis`."""

    content: str
    action: Literal["write", "explain", "debug"]


class DocparseMessageRequest(RequestModel):
    """Вопрос по разобранному документу."""

    content: str


class SqlRegenerateRequest(RequestModel):
    """Необязательное тело повторной генерации в диалоге `sql`: замена схемы (§5.6)."""

    schema_id: UUID | None


class DocparseFieldOut(BaseModel):
    """Реквизит разбора; `value: null` — в документе его нет."""

    title: str
    value: str | None


class DocparseOut(BaseModel):
    """Результат разбора — поле `docparse` диалога (§7.3)."""

    file_name: str
    page_count: int | None
    template_id: str
    template_title: str
    free_form: bool
    fields: list[DocparseFieldOut]
    summary: str
    summary_status: SummaryStatus

    @classmethod
    def of(cls, docparse: Docparse) -> "DocparseOut":
        """Собрать из разбора; пока содержание пишется, поле `summary` пусто."""
        return cls(
            file_name=docparse.file_name,
            page_count=docparse.page_count,
            template_id=docparse.template_id,
            template_title=docparse.template_title,
            free_form=docparse.free_form,
            fields=[DocparseFieldOut.model_validate(item) for item in docparse.fields],
            summary=docparse.summary,
            summary_status=docparse.summary_status,
        )


class DialogOut(BaseModel):
    """Объект `Dialog`."""

    id: UUID
    kind: DialogKind
    title: str | None
    created_at: ApiTime
    updated_at: ApiTime

    @classmethod
    def of(cls, dialog: Dialog) -> "DialogOut":
        """Собрать из диалога."""
        return cls(
            id=dialog.id,
            kind=dialog.kind,
            title=dialog.title,
            created_at=dialog.created_at,
            updated_at=dialog.updated_at,
        )


class DialogDetailsOut(DialogOut):
    """`GET /api/dialogs/{id}` для вида `docparse`: диалог с результатом разбора."""

    docparse: DocparseOut


class DialogListOut(BaseModel):
    """Часть списка диалогов."""

    items: list[DialogOut]
    next_cursor: str | None


class AttachmentOut(BaseModel):
    """Объект `Attachment`."""

    id: UUID
    file_name: str
    media_type: MediaType
    page_count: int | None
    image_count: int
    created_at: ApiTime

    @classmethod
    def of(cls, attachment: Attachment) -> "AttachmentOut":
        """Собрать из вложения."""
        return cls(
            id=attachment.id,
            file_name=attachment.file_name,
            media_type=attachment.media_type,
            page_count=attachment.page_count,
            image_count=attachment.image_count,
            created_at=attachment.created_at,
        )


class SourceOut(BaseModel):
    """Объект `Source`: источник, на который в ответе есть сноска `[n]`."""

    n: int
    document_id: UUID
    document_title: str
    scope: Literal["shared", "personal"]
    page: int | None
    fragment_id: UUID
    quote: str


class MessageOut(BaseModel):
    """Объект `Message`."""

    id: UUID
    role: MessageRole
    content: str
    status: MessageStatus
    error_code: str | None
    reasoning: str | None
    reasoning_seconds: int | None
    attachments: list[AttachmentOut]
    sources: list[SourceOut] | None
    sources_found: int | None
    sql_check: dict[str, Any] | None
    sql_dangers: list[str] | None
    dropped_messages: int
    created_at: ApiTime

    @classmethod
    def of(cls, view: MessageView) -> "MessageOut":
        """Собрать из сообщения с вложениями."""
        message = view.message
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            status=message.status,
            error_code=message.error_code,
            reasoning=message.reasoning,
            reasoning_seconds=message.reasoning_seconds,
            attachments=[AttachmentOut.of(item) for item in view.attachments],
            sources=None
            if message.sources is None
            else [SourceOut.model_validate(source) for source in message.sources],
            sources_found=message.sources_found,
            sql_check=message.sql_check,
            sql_dangers=message.sql_dangers,
            dropped_messages=message.dropped_messages,
            created_at=message.created_at,
        )


class MessageListOut(BaseModel):
    """Часть списка сообщений."""

    items: list[MessageOut]
    next_cursor: str | None
