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
from portal.core.container import Container
from portal.core.errors import feature_disabled, field_error, not_found, validation_error
from portal.core.sse import event_stream_response
from portal.core.uploads import receive_recording
from portal.core.validation import validation_failure
from portal.dialogs.domain import DialogKind
from portal.dialogs.generation import UNSET, MemoryQuery, QuestionInput, _Unset
from portal.dialogs.schemas import (
    AttachmentOut,
    ChatMemoryQuery,
    ChatMessageRequest,
    CodeMemoryQuery,
    CodeMessageRequest,
    CogisMemoryQuery,
    CogisMessageRequest,
    CreateDialogRequest,
    DialogListOut,
    DialogMemoryOut,
    DialogOut,
    DocparseMessageRequest,
    DraftRequest,
    MemoryModeRequest,
    MemoryOut,
    MessageListOut,
    MessageOut,
    RenameDialogRequest,
    SqlMemoryQuery,
    SqlMessageRequest,
    SqlRegenerateRequest,
    SummaryOut,
    SummaryRequest,
)
from portal.dialogs.service import CarriedInput
from portal.files.ports import DOCX

router = APIRouter(prefix="/api/dialogs")

# Маршрут загрузки файла: у него свой предел размера тела (§1.3).
ATTACHMENT_UPLOAD_PATH = re.compile(r"/api/dialogs/[^/]+/attachments")
# Маршрут загрузки записи: тело — сам файл, предел — `media.max_bytes` (§1.6).
RECORDING_UPLOAD_PATH = re.compile(r"/api/dialogs/[^/]+/attachments/media")

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise not_found() from None


async def dialog_id(id: str, user: Employee, container: ContainerDep) -> UUID:
    """Идентификатор диалога из пути; сессия и роль проверяются раньше ресурса (§1.2).

    При выключенном разделе «Код» диалоги `code` закрыты всеми маршрутами (§7.5.15).
    """
    target = _uuid(id)
    if not container.settings.code.enabled:
        dialog = await container.dialogs.find(user.id, target)
        if dialog is not None and dialog.kind == "code":
            raise feature_disabled()
    return target


def attachment_id(attachment_id: str, _: Employee) -> UUID:
    """Идентификатор вложения из пути."""
    return _uuid(attachment_id)


def message_id(message_id: str, _: Employee) -> UUID:
    """Идентификатор сообщения из пути."""
    return _uuid(message_id)


DialogId = Annotated[UUID, Depends(dialog_id)]
AttachmentId = Annotated[UUID, Depends(attachment_id)]
MessageId = Annotated[UUID, Depends(message_id)]
Limit = Annotated[int | None, Query(ge=1, le=100)]


@router.get("")
async def list_dialogs(
    user: Employee,
    container: ContainerDep,
    kind: DialogKind,
    limit: Limit = None,
    cursor: str | None = None,
) -> DialogListOut:
    """Диалоги одного вида, от недавно обновлённых; диалоги `code` — только через проект."""
    if kind == "code":
        raise validation_error([field_error("kind", "unknown_value")])
    page = await container.dialogs.list(user.id, kind, limit or 30, cursor)
    return DialogListOut(
        items=[DialogOut.of(dialog) for dialog in page.items], next_cursor=page.next_cursor
    )


@router.post("", status_code=201)
async def create_dialog(
    body: CreateDialogRequest, user: Employee, container: ContainerDep
) -> DialogOut:
    """Создать диалог; с `carried` — перенос из своего диалога (§5.10.7)."""
    if body.kind == "cogis" and container.settings.code.enabled:
        # Прежний помощник CoGIS при разделе «Код» только для чтения (§7.5.15).
        raise validation_error([field_error("kind", "unknown_value")])
    carried = None
    if body.carried is not None:
        if not container.settings.memory.enabled:
            raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
        carried = CarriedInput(body.carried.text, body.carried.source_dialog_id)
    return DialogOut.of(await container.dialogs.create(user.id, body.kind, carried))


@router.get("/{id}", response_model=None)
async def get_dialog(target: DialogId, user: Employee, container: ContainerDep) -> DialogMemoryOut:
    """Диалог владельца с пересказом и перенесённым; у разбора — и с его результатом."""
    return DialogMemoryOut.of_details(await container.dialogs.details(user.id, target))


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


async def _question(
    container: Container, owner_id: UUID, dialog_id: UUID, kind: DialogKind, body: dict[str, Any]
) -> QuestionInput:
    """Разобрать тело сообщения по виду диалога (§5.3); чужие виду поля — ошибка."""
    if kind == "code":
        try:
            code = CodeMessageRequest.model_validate(body)
        except ValidationError as error:
            raise validation_failure(error.errors()) from None
        target = None if code.target is None else code.target.model_dump()
        params = await container.code_tool.question_params(
            owner_id, dialog_id, code.action, code.file_ids, target
        )
        return QuestionInput(code.content, params)
    try:
        if kind == "chat":
            chat = ChatMessageRequest.model_validate(body)
            params = {
                "mode": chat.mode,
                "collections": list(chat.collections),
                "tags": list(chat.tags),
            }
            return QuestionInput(chat.content, params, chat.attachment_ids)
        if kind == "sql":
            sql = SqlMessageRequest.model_validate(body)
            schema_id = None if sql.schema_id is None else str(sql.schema_id)
            return QuestionInput(
                sql.content,
                {
                    "action": sql.action,
                    "dialect": sql.dialect,
                    "schema_id": schema_id,
                    "test_run": sql.test_run,
                },
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
    dialog = await container.dialogs.writable(user.id, target)
    question = await _question(container, user.id, target, dialog.kind, body)
    channel = await container.generation.send(
        user, session.session_id, target, dialog.kind, question
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
    dialog = await container.dialogs.writable(user.id, target)
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


async def _memory_query(
    container: Container, owner_id: UUID, dialog_id: UUID, kind: DialogKind, query: dict[str, str]
) -> MemoryQuery:
    """Разобрать строку запроса оценки по виду диалога (§5.10.3); чужие параметры не читаются."""
    if kind == "code":
        try:
            code = CodeMemoryQuery.model_validate(query)
        except ValidationError as error:
            raise validation_failure(error.errors()) from None
        return MemoryQuery(
            await container.code_tool.memory_params(owner_id, dialog_id, code.action, code.files)
        )
    try:
        if kind == "chat":
            chat = ChatMemoryQuery.model_validate(query)
            params: dict[str, Any] = {"mode": chat.mode, "collections": [], "tags": []}
            return MemoryQuery(params, search=chat.search == "true")
        if kind == "sql":
            sql = SqlMemoryQuery.model_validate(query)
            schema_id = None if sql.schema_id is None else str(sql.schema_id)
            return MemoryQuery(
                {
                    "action": sql.action,
                    "dialect": sql.dialect,
                    "schema_id": schema_id,
                    "test_run": False,
                }
            )
        if kind == "cogis":
            return MemoryQuery({"action": CogisMemoryQuery.model_validate(query).action})
        return MemoryQuery({})
    except ValidationError as error:
        raise validation_failure(error.errors()) from None


@router.get("/{id}/memory")
async def get_memory(
    request: Request, target: DialogId, user: Employee, container: ContainerDep
) -> MemoryOut:
    """Оценка памяти диалога для следующего вопроса: индикатор и панель (§5.10.3)."""
    if not container.settings.memory.enabled:
        raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
    dialog = await container.dialogs.get(user.id, target)
    query = await _memory_query(container, user.id, target, dialog.kind, dict(request.query_params))
    return MemoryOut.of(await container.generation.memory(user.id, target, dialog.kind, query))


@router.patch("/{id}/messages/{message_id}")
async def set_memory_mode(
    body: dict[str, Any],
    target: DialogId,
    message: MessageId,
    user: Employee,
    container: ContainerDep,
) -> MessageOut:
    """Сменить режим пары «вопрос + ответ»; `message_id` — вопрос (§5.10.4)."""
    if not container.settings.memory.enabled:
        raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
    await container.dialogs.writable(user.id, target)
    try:
        mode = MemoryModeRequest.model_validate(body).memory_mode
    except ValidationError as error:
        raise validation_failure(error.errors()) from None
    view = await container.generation.set_memory_mode(user.id, target, message, mode)
    return MessageOut.of(view)


@router.post("/{id}/memory/draft")
async def memory_draft(
    body: dict[str, Any],
    target: DialogId,
    user: Employee,
    session: ReadySession,
    container: ContainerDep,
) -> StreamingResponse:
    """Черновик пересказа для сжатия или переноса; ответ — поток событий (§5.10.5, §6.5)."""
    if not container.settings.memory.enabled:
        raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
    dialog = await container.dialogs.get(user.id, target)
    try:
        draft = DraftRequest.model_validate(body)
    except ValidationError as error:
        raise validation_failure(error.errors()) from None
    if draft.purpose == "compress":
        container.dialogs.check_writable(dialog)  # перенос из диалога CoGIS работает (§7.5.15)
    if draft.purpose == "compress" and "until_message_id" in body:
        # Граница сжатия — не выбор сотрудника: её считает сервер (§5.10.5).
        raise validation_error([field_error("until_message_id", "invalid_format")])
    channel = await container.drafts.start(
        user.id, session.session_id, target, draft.purpose, draft.until_message_id
    )
    return event_stream_response(channel, container.settings.dialogs.keepalive_seconds)


@router.put("/{id}/memory/summary")
async def put_summary(
    body: dict[str, Any], target: DialogId, user: Employee, container: ContainerDep
) -> SummaryOut:
    """Применить черновик или изменить применённый пересказ (§5.10.6)."""
    if not container.settings.memory.enabled:
        raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
    await container.dialogs.writable(user.id, target)
    try:
        summary = SummaryRequest.model_validate(body)
    except ValidationError as error:
        raise validation_failure(error.errors()) from None
    view = await container.generation.set_summary(
        user.id, target, summary.text, summary.until_message_id
    )
    return SummaryOut.of(view)


@router.delete("/{id}/memory/summary", status_code=204)
async def delete_summary(target: DialogId, user: Employee, container: ContainerDep) -> Response:
    """Отменить сжатие: пары снова идут модели в своих режимах (§5.10.6)."""
    if not container.settings.memory.enabled:
        raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
    await container.dialogs.writable(user.id, target)
    await container.generation.clear_summary(user.id, target)
    return Response(status_code=204)


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


@router.post("/{id}/attachments/media", status_code=201)
async def upload_recording(
    request: Request,
    target: DialogId,
    user: Employee,
    session: ReadySession,
    container: ContainerDep,
    file_name: str | None = None,
) -> AttachmentOut:
    """Загрузить запись (аудио или видео) телом запроса; расшифровка идёт в очереди (§5.8).

    Порядок проверок до чтения тела — §1.6: флаг функции, диалог, параметры строки запроса.
    """
    if not container.settings.media.enabled:
        raise feature_disabled()
    await container.dialogs.require_chat(user.id, target)
    if not file_name:
        raise validation_error([field_error("file_name", "required")])
    recording = await receive_recording(request, container, session)
    attachment = await container.dialogs.add_recording(
        user.id, session.session_id, target, file_name, recording
    )
    return AttachmentOut.of(attachment)


@router.get("/{id}/attachments/{attachment_id}")
async def get_attachment(
    target: DialogId, attachment: AttachmentId, user: Employee, container: ContainerDep
) -> AttachmentOut:
    """Вложение диалога: по нему интерфейс следит за расшифровкой записи (§5.8)."""
    return AttachmentOut.of(await container.dialogs.get_attachment(user.id, target, attachment))


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
    found, path = await container.dialogs.attachment_file(user.id, target, attachment)
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
