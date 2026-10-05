"""Маршруты диалогов, сообщений и вложений (docs/portal-api.md §5.2); уровень «сотрудник»."""

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, StreamingResponse

from portal.core.access import ContainerDep, Employee, ReadySession
from portal.core.errors import field_error, not_found, validation_error
from portal.core.sse import event_stream_response
from portal.core.validation import validation_failure
from portal.dialogs.domain import DialogKind
from portal.dialogs.generation import UNSET, QuestionInput, _Unset
from portal.dialogs.schemas import (
    AttachmentOut,
    ChatMessageRequest,
    CogisMessageRequest,
    CreateDialogRequest,
    DialogDetailsOut,
    DialogListOut,
    DialogOut,
    DocparseMessageRequest,
    DocparseOut,
    MessageListOut,
    MessageOut,
    RenameDialogRequest,
    SqlMessageRequest,
    SqlRegenerateRequest,
)
from portal.files.ports import DOCX

router = APIRouter(prefix="/api/dialogs")

# Маршрут загрузки файла: у него свой предел размера тела (§1.3).
ATTACHMENT_UPLOAD_PATH = re.compile(r"/api/dialogs/[^/]+/attachments")

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise not_found() from None


def dialog_id(id: str, _: Employee) -> UUID:
    """Идентификатор диалога из пути; сессия и роль проверяются раньше ресурса (§1.2)."""
    return _uuid(id)


def attachment_id(attachment_id: str, _: Employee) -> UUID:
    """Идентификатор вложения из пути."""
    return _uuid(attachment_id)


DialogId = Annotated[UUID, Depends(dialog_id)]
AttachmentId = Annotated[UUID, Depends(attachment_id)]
Limit = Annotated[int | None, Query(ge=1, le=100)]


@router.get("")
async def list_dialogs(
    user: Employee,
    container: ContainerDep,
    kind: DialogKind,
    limit: Limit = None,
    cursor: str | None = None,
) -> DialogListOut:
    """Диалоги одного вида, от недавно обновлённых."""
    page = await container.dialogs.list(user.id, kind, limit or 30, cursor)
    return DialogListOut(
        items=[DialogOut.of(dialog) for dialog in page.items], next_cursor=page.next_cursor
    )


@router.post("", status_code=201)
async def create_dialog(
    body: CreateDialogRequest, user: Employee, container: ContainerDep
) -> DialogOut:
    """Создать диалог."""
    return DialogOut.of(await container.dialogs.create(user.id, body.kind))


@router.get("/{id}", response_model=None)
async def get_dialog(
    target: DialogId, user: Employee, container: ContainerDep
) -> DialogOut | DialogDetailsOut:
    """Диалог владельца; у разбора документа — вместе с его результатом."""
    dialog, docparse = await container.dialogs.get_with_docparse(user.id, target)
    if docparse is None:
        return DialogOut.of(dialog)
    return DialogDetailsOut(**DialogOut.of(dialog).model_dump(), docparse=DocparseOut.of(docparse))


@router.patch("/{id}")
async def rename_dialog(
    body: RenameDialogRequest, target: DialogId, user: Employee, container: ContainerDep
) -> DialogOut:
    """Переименовать диалог."""
    return DialogOut.of(await container.dialogs.rename(user.id, target, body.title))


@router.delete("/{id}", status_code=204)
async def delete_dialog(target: DialogId, user: Employee, container: ContainerDep) -> Response:
    """Удалить диалог с сообщениями и вложениями."""
    await container.dialogs.delete(user.id, target)
    return Response(status_code=204)


@router.get("/{id}/messages")
async def list_messages(
    target: DialogId,
    user: Employee,
    container: ContainerDep,
    limit: Limit = None,
    cursor: str | None = None,
) -> MessageListOut:
    """Сообщения диалога, от новых к старым."""
    page = await container.dialogs.messages(user.id, target, limit or 50, cursor)
    return MessageListOut(
        items=[MessageOut.of(view) for view in page.items], next_cursor=page.next_cursor
    )


def _question(kind: DialogKind, body: dict[str, Any]) -> QuestionInput:
    """Разобрать тело сообщения по виду диалога (§5.3); чужие виду поля — ошибка."""
    try:
        if kind == "chat":
            chat = ChatMessageRequest.model_validate(body)
            params = {"mode": chat.mode, "knowledge": chat.knowledge}
            return QuestionInput(chat.content, params, chat.attachment_ids)
        if kind == "sql":
            sql = SqlMessageRequest.model_validate(body)
            schema_id = None if sql.schema_id is None else str(sql.schema_id)
            return QuestionInput(
                sql.content, {"action": sql.action, "dialect": sql.dialect, "schema_id": schema_id}
            )
        if kind == "cogis":
            cogis = CogisMessageRequest.model_validate(body)
            return QuestionInput(cogis.content, {"action": cogis.action})
        return QuestionInput(DocparseMessageRequest.model_validate(body).content, {})
    except ValidationError as error:
        raise validation_failure(error.errors()) from None


@router.post("/{id}/messages")
async def send_message(
    body: dict[str, Any],
    target: DialogId,
    user: Employee,
    session: ReadySession,
    container: ContainerDep,
) -> StreamingResponse:
    """Отправить вопрос; ответ — поток событий (§6).

    Тело разбирается после поиска диалога: его состав зависит от вида диалога (§5.3).
    """
    dialog = await container.dialogs.get(user.id, target)
    channel = await container.generation.send(
        user, session.session_id, target, dialog.kind, _question(dialog.kind, body)
    )
    return event_stream_response(channel, container.settings.dialogs.keepalive_seconds)


@router.post("/{id}/regenerate")
async def regenerate(
    request: Request,
    target: DialogId,
    user: Employee,
    session: ReadySession,
    container: ContainerDep,
) -> StreamingResponse:
    """Сформировать последний ответ заново; ответ — поток событий (§6).

    Тело читается только в диалоге `sql`: там оно может заменить схему базы (§5.6).
    """
    dialog = await container.dialogs.get(user.id, target)
    schema_id: UUID | _Unset | None = UNSET
    if dialog.kind == "sql" and (raw := await request.body()).strip():
        try:
            schema_id = SqlRegenerateRequest.model_validate_json(raw).schema_id
        except ValidationError as error:
            raise validation_failure(error.errors()) from None
    channel = await container.generation.regenerate(
        user, session.session_id, target, dialog.kind, schema_id
    )
    return event_stream_response(channel, container.settings.dialogs.keepalive_seconds)


@router.get("/{id}/export")
async def export_dialog(
    target: DialogId, user: Employee, container: ContainerDep, message_id: str | None = None
) -> Response:
    """Файл DOCX: диалог целиком или один ответ (§5.9)."""
    answer_id = None if message_id is None else _uuid(message_id)
    file_name, content = await container.dialogs.export(user.id, target, answer_id)
    return Response(
        content,
        media_type=DOCX,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"dialog.docx\"; filename*=UTF-8''{quote(file_name)}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        yield chunk


@router.post("/{id}/attachments", status_code=201)
async def upload_attachment(
    request: Request, target: DialogId, user: Employee, container: ContainerDep
) -> AttachmentOut:
    """Загрузить файл в чат до отправки сообщения."""
    # Диалог и его вид проверяются до разбора формы: чужой файл не принимается вовсе.
    await container.dialogs.require_chat(user.id, target)
    async with request.form(max_files=1, max_fields=1) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise validation_error([field_error("file", "required")])
        attachment = await container.dialogs.add_attachment(
            user.id, target, file.filename or "", _chunks(file)
        )
    return AttachmentOut.of(attachment)


@router.delete("/{id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    target: DialogId, attachment: AttachmentId, user: Employee, container: ContainerDep
) -> Response:
    """Удалить неотправленное вложение."""
    await container.dialogs.delete_attachment(user.id, target, attachment)
    return Response(status_code=204)


@router.get("/{id}/attachments/{attachment_id}/file")
async def attachment_file(
    target: DialogId, attachment: AttachmentId, user: Employee, container: ContainerDep
) -> FileResponse:
    """Оригинал вложения владельцу диалога (§1.5)."""
    found, path = await container.dialogs.get_attachment(user.id, target, attachment)
    is_text = found.media_type.startswith("text/")
    disposition = "attachment" if found.media_type == DOCX else "inline"
    return FileResponse(
        path,
        media_type=_TEXT_CONTENT_TYPE if is_text else found.media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(found.file_name)}",
        },
    )
