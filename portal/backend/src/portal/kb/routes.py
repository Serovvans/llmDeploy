"""Маршруты базы знаний (docs/portal-api.md §8.1); уровень «сотрудник».

Администратор здесь — обычный сотрудник с отличиями только в правах на изменение:
распоряжаться документом общей базы и вести «Министерства». Чужой личный документ и его
теги для него не существуют — `404`.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, StreamingResponse

from portal.core.access import ContainerDep, Employee, PageQueryDep, ReadySession
from portal.core.errors import (
    AppError,
    feature_disabled,
    field_error,
    not_found,
    validation_error,
)
from portal.core.middleware import error_response
from portal.core.uploads import receive_recording
from portal.files.ports import DOCX
from portal.kb import errors
from portal.kb.domain import Scope
from portal.kb.schemas import (
    CogisDocumentationOut,
    DocumentTextOut,
    DuplicateOut,
    KbDocumentOut,
    KbDocumentPageOut,
    TagsIn,
    TagsOut,
)
from portal.kb.service import DuplicateDocumentError

router = APIRouter(prefix="/api/kb")

# Маршрут загрузки файла: у него свой предел размера тела (§1.3).
DOCUMENT_UPLOAD_PATH = re.compile(r"/api/kb/documents")
# Маршрут загрузки записи: тело — сам файл, предел — `media.max_bytes` (§1.6).
RECORDING_UPLOAD_PATH = re.compile(r"/api/kb/documents/media")

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
_AUDIO_CONTENT_TYPE = "audio/ogg"
_AUDIO_CHUNK_BYTES = 256 * 1024
# Один диапазон байт: `a-b`, `a-` или `-n` (§8.7). Число длиннее 19 цифр больше любого
# файла и шаблону не подходит: такой заголовок игнорируется, как и любой нечитаемый.
_RANGE = re.compile(r"bytes=(\d{0,19})-(\d{0,19})")
# Наибольшее значение целого столбца PostgreSQL: номер страницы и таймкод больше — ошибка
# параметра, а не запроса к базе.
_INT_MAX = 2**31 - 1
_SCOPES: tuple[Scope, ...] = ("shared", "personal", "ministries")
_FLAGS = {"true": True, "false": False}


def document_id(id: str, _: Employee) -> UUID:
    """Идентификатор документа из пути; сессия и роль проверяются раньше ресурса (§1.2)."""
    try:
        return UUID(id)
    except ValueError:
        raise not_found() from None


DocumentId = Annotated[UUID, Depends(document_id)]


@router.get("/documents")
async def list_documents(
    user: Employee,
    container: ContainerDep,
    scope: Scope,
    query: PageQueryDep,
    tags: Annotated[list[str] | None, Query()] = None,
) -> KbDocumentPageOut:
    """Страница документов коллекции; `tags` — только документы с одним из тегов."""
    page = await container.kb.list_documents(user, scope, query, tags or ())
    return KbDocumentPageOut(
        items=[KbDocumentOut.of(view, user) for view in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
    )


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        yield chunk


def _choice[T](form_value: object, field: str, choices: dict[str, T]) -> T:
    """Значение поля формы из закрытого списка; иначе — ошибка этого поля."""
    if not isinstance(form_value, str):
        raise validation_error([field_error(field, "required")])
    if form_value not in choices:
        raise validation_error([field_error(field, "unknown_value")])
    return choices[form_value]


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request, user: Employee, container: ContainerDep
) -> KbDocumentOut:
    """Добавить документ: файл принимается потоком и ставится в очередь индексации."""
    # Число полей `tags` разбор формы не ограничивает: лишние теги — отказ `too_many` по
    # полю `tags` (§8.5), а размер тела ограничен пределом загрузки.
    async with request.form(max_files=1) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise validation_error([field_error("file", "required")])
        scope = _choice(form.get("scope"), "scope", {value: value for value in _SCOPES})
        is_cogis = _choice(form.get("is_cogis"), "is_cogis", _FLAGS)
        tags = [tag for tag in form.getlist("tags") if isinstance(tag, str)]
        try:
            view = await container.kb.add(
                user, file.filename or "", _chunks(file), scope, is_cogis, tags
            )
        except DuplicateDocumentError as duplicate:
            details = DuplicateOut.of(duplicate.existing, user).model_dump(mode="json")
            raise errors.duplicate_document(details) from None
    return KbDocumentOut.of(view, user)


@router.post("/documents/media", status_code=201)
async def upload_recording(
    request: Request,
    user: Employee,
    session: ReadySession,
    container: ContainerDep,
    scope: str | None = None,
    file_name: str | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
) -> KbDocumentOut:
    """Добавить запись (аудио или видео) телом запроса; расшифровка идёт в очереди (§8.1).

    Порядок проверок до чтения тела — §1.6: флаг функции, право на коллекцию, параметры
    строки запроса. Параметры объявлены необязательными, чтобы их отсутствие проверялось
    после флага и права, а не раньше.
    """
    if not container.settings.media.enabled:
        raise feature_disabled()
    target = container.kb.recording_target(user, scope, file_name, tags or ())
    recording = await receive_recording(request, container, session)
    try:
        view = await container.kb.add_recording(user, session.session_id, target, recording)
    except DuplicateDocumentError as duplicate:
        details = DuplicateOut.of(duplicate.existing, user).model_dump(mode="json")
        raise errors.duplicate_document(details) from None
    return KbDocumentOut.of(view, user)


@router.get("/documents/{id}")
async def get_document(
    target: DocumentId, user: Employee, container: ContainerDep
) -> KbDocumentOut:
    """Документ, видимый пользователю."""
    return KbDocumentOut.of(await container.kb.get(user, target), user)


@router.delete("/documents/{id}", status_code=204)
async def delete_document(target: DocumentId, user: Employee, container: ContainerDep) -> Response:
    """Удалить документ: право — по коллекции и текущей роли (§8.1)."""
    await container.kb.delete(user, target)
    return Response(status_code=204)


@router.post("/documents/{id}/retry")
async def retry_document(
    target: DocumentId, user: Employee, container: ContainerDep
) -> KbDocumentOut:
    """Отправить документ с ошибкой на повторную обработку."""
    return KbDocumentOut.of(await container.kb.retry(user, target), user)


@router.put("/documents/{id}/tags")
async def set_document_tags(
    target: DocumentId, body: TagsIn, user: Employee, container: ContainerDep
) -> KbDocumentOut:
    """Заменить теги документа целиком (§8.5); `[]` — снять все."""
    return KbDocumentOut.of(await container.kb.set_tags(user, target, body.tags), user)


@router.get("/tags")
async def suggest_tags(
    user: Employee,
    container: ContainerDep,
    scope: Annotated[list[Scope] | None, Query()] = None,
    q: str = "",
) -> TagsOut:
    """Подсказки: теги документов, видимых пользователю, в выбранных коллекциях (§8.5)."""
    return TagsOut(items=await container.kb.suggest_tags(user, scope or (), q))


@router.get("/documents/{id}/text")
async def document_text(
    target: DocumentId,
    user: Employee,
    container: ContainerDep,
    page: Annotated[int | None, Query(le=_INT_MAX)] = None,
    fragment_id: UUID | None = None,
    timecode: Annotated[int | None, Query(ge=0, le=_INT_MAX)] = None,
) -> DocumentTextOut:
    """Текст страницы с подсвеченной цитатой (§8.3); `timecode` — момент записи, секунды."""
    return DocumentTextOut.of(await container.kb.text(user, target, page, fragment_id, timecode))


@router.get("/documents/{id}/file")
async def document_file(
    target: DocumentId, user: Employee, container: ContainerDep
) -> FileResponse:
    """Оригинал документа (§1.5, §8.2)."""
    document, media_type, path = await container.kb.file(user, target)
    is_text = media_type.startswith("text/")
    disposition = "attachment" if media_type == DOCX else "inline"
    return FileResponse(
        path,
        media_type=_TEXT_CONTENT_TYPE if is_text else media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(document.title)}",
        },
    )


@router.get("/documents/{id}/images/{image_id}")
async def document_image(
    target: DocumentId, image_id: str, user: Employee, container: ContainerDep
) -> FileResponse:
    """Картинка документа (§8.6): только `image/png` или `image/jpeg` из строки картинки."""
    try:
        image_uuid = UUID(image_id)
    except ValueError:
        raise not_found() from None
    image, path = await container.kb.image(user, target, image_uuid)
    return FileResponse(
        path,
        media_type=image.media_type,
        headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"},
    )


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Запрошенный диапазон `(первый байт, последний байт)`; `None` — отдать файл целиком.

    Несколько диапазонов и нечитаемый заголовок игнорируются. Диапазон за пределами
    файла — `range_not_satisfiable` (§8.7).
    """
    match = _RANGE.fullmatch(header.strip()) if header else None
    if match is None or not (match[1] or match[2]):
        return None
    if not match[1]:  # `-n`: последние n байт
        suffix = int(match[2])
        if suffix == 0 or size == 0:
            raise errors.range_not_satisfiable()
        return max(size - suffix, 0), size - 1
    first = int(match[1])
    last = min(int(match[2]), size - 1) if match[2] else size - 1
    if first >= size or first > last:
        raise errors.range_not_satisfiable()
    return first, last


async def _file_bytes(path: Path, first: int, length: int) -> AsyncIterator[bytes]:
    """Байты файла с позиции `first`, всего `length`."""
    with path.open("rb") as file:
        file.seek(first)
        while length > 0:
            chunk = await asyncio.to_thread(file.read, min(_AUDIO_CHUNK_BYTES, length))
            if not chunk:
                return
            length -= len(chunk)
            yield chunk


@router.get("/documents/{id}/audio")
async def document_audio(
    request: Request, target: DocumentId, user: Employee, container: ContainerDep
) -> Response:
    """Сжатый звук записи (§8.7): целиком либо один диапазон байт — для перемотки."""
    path = await container.kb.audio(user, target)
    size = (await asyncio.to_thread(path.stat)).st_size
    headers = {
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    }
    try:
        requested = _byte_range(request.headers.get("range"), size)
    except AppError as refusal:
        response = error_response(refusal)
        response.headers["Content-Range"] = f"bytes */{size}"
        return response
    first, last = requested or (0, size - 1)
    length = last - first + 1
    headers["Content-Length"] = str(length)
    if requested is not None:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    return StreamingResponse(
        _file_bytes(path, first, length),
        status_code=200 if requested is None else 206,
        media_type=_AUDIO_CONTENT_TYPE,
        headers=headers,
    )


@router.get("/cogis-documentation")
async def cogis_documentation(_: Employee, container: ContainerDep) -> CogisDocumentationOut:
    """Есть ли в общей базе готовая документация CoGIS (§7.2)."""
    return CogisDocumentationOut(available=await container.kb.has_cogis_documentation())
