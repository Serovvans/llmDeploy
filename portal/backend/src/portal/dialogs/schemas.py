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
    Knowledge,
    MessageRole,
    MessageStatus,
)
from portal.dialogs.service import MessageView
from portal.files.ports import MediaType


class CreateDialogRequest(RequestModel):
    """`POST /api/dialogs`; виды `sql` и `cogis` появятся на этапе 5."""

    kind: Literal["chat"]


class RenameDialogRequest(RequestModel):
    """`PATCH /api/dialogs/{id}`."""

    title: str


class ChatMessageRequest(RequestModel):
    """Тело сообщения в диалоге `chat` (§5.3)."""

    content: str
    attachment_ids: tuple[UUID, ...] = ()
    mode: AnswerMode
    knowledge: Knowledge


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
    """Объект `Message`; поля проверки SQL пока всегда `null` — диалоги `sql` на этапе 5."""

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
    sql_check: dict[str, Any] | None = None
    sql_dangers: list[str] | None = None
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
            dropped_messages=message.dropped_messages,
            created_at=message.created_at,
        )


class MessageListOut(BaseModel):
    """Часть списка сообщений."""

    items: list[MessageOut]
    next_cursor: str | None
