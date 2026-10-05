"""Маршруты базы знаний (docs/portal-api.md §8.1); уровень «сотрудник».

Администратор здесь — обычный сотрудник с одним отличием: правом удалить общий документ
и отправить его на повторную обработку. Чужой личный документ для него — `404`.
"""

import re
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse

from portal.core.access import ContainerDep, Employee, PageQueryDep
from portal.core.errors import field_error, not_found, validation_error
from portal.files.ports import DOCX
from portal.kb import errors
from portal.kb.domain import Scope
from portal.kb.schemas import (
    CogisDocumentationOut,
    DocumentTextOut,
    DuplicateOut,
    KbDocumentOut,
    KbDocumentPageOut,
)
from portal.kb.service import DuplicateDocumentError

router = APIRouter(prefix="/api/kb")

# Маршрут загрузки файла: у него свой предел размера тела (§1.3).
DOCUMENT_UPLOAD_PATH = re.compile(r"/api/kb/documents")

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
_SCOPES: tuple[Scope, ...] = ("shared", "personal")
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
) -> KbDocumentPageOut:
    """Страница документов общей базы или личной базы пользователя."""
    page = await container.kb.list_documents(user, scope, query)
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
    async with request.form(max_files=1, max_fields=2) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise validation_error([field_error("file", "required")])
        scope = _choice(form.get("scope"), "scope", {value: value for value in _SCOPES})
        is_cogis = _choice(form.get("is_cogis"), "is_cogis", _FLAGS)
        try:
            view = await container.kb.add(user, file.filename or "", _chunks(file), scope, is_cogis)
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
    """Удалить документ: автор либо администратор (только общие)."""
    await container.kb.delete(user, target)
    return Response(status_code=204)


@router.post("/documents/{id}/retry")
async def retry_document(
    target: DocumentId, user: Employee, container: ContainerDep
) -> KbDocumentOut:
    """Отправить документ с ошибкой на повторную обработку."""
    return KbDocumentOut.of(await container.kb.retry(user, target), user)


@router.get("/documents/{id}/text")
async def document_text(
    target: DocumentId,
    user: Employee,
    container: ContainerDep,
    page: int | None = None,
    fragment_id: UUID | None = None,
) -> DocumentTextOut:
    """Текст страницы с подсвеченной цитатой (§8.3)."""
    return DocumentTextOut.of(await container.kb.text(user, target, page, fragment_id))


@router.get("/documents/{id}/file")
async def document_file(
    target: DocumentId, user: Employee, container: ContainerDep
) -> FileResponse:
    """Оригинал документа (§1.5, §8.2)."""
    document, path = await container.kb.file(user, target)
    is_text = document.media_type.startswith("text/")
    disposition = "attachment" if document.media_type == DOCX else "inline"
    return FileResponse(
        path,
        media_type=_TEXT_CONTENT_TYPE if is_text else document.media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(document.title)}",
        },
    )


@router.get("/cogis-documentation")
async def cogis_documentation(_: Employee, container: ContainerDep) -> CogisDocumentationOut:
    """Есть ли в общей базе готовая документация CoGIS (§7.2)."""
    return CogisDocumentationOut(available=await container.kb.has_cogis_documentation())
