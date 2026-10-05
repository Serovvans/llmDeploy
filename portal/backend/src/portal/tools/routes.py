"""Маршруты инструментов: схемы SQL и запуск разбора документа (§7.1, §7.3)."""

import re
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import UploadFile
from starlette.responses import StreamingResponse

from portal.core.access import ContainerDep, Employee, ReadySession
from portal.core.errors import field_error, not_found, validation_error
from portal.core.sse import event_stream_response
from portal.tools.schemas import (
    SqlSchemaListOut,
    SqlSchemaOut,
    SqlSchemaRequest,
    SqlSchemaSummaryOut,
)

router = APIRouter(prefix="/api")

# Маршрут загрузки файла: у него свой предел размера тела (§1.3).
DOCPARSE_UPLOAD_PATH = re.compile(r"/api/docparse")

_UPLOAD_CHUNK_BYTES = 1024 * 1024


def schema_id(id: str, _: Employee) -> UUID:
    """Идентификатор схемы из пути; сессия и роль проверяются раньше ресурса (§1.2)."""
    try:
        return UUID(id)
    except ValueError:
        raise not_found() from None


SchemaId = Annotated[UUID, Depends(schema_id)]


@router.get("/sql/schemas")
async def list_schemas(user: Employee, container: ContainerDep) -> SqlSchemaListOut:
    """Все схемы пользователя по названию; текст схем в списке не отдаётся."""
    schemas = await container.sql_schemas.list(user.id)
    return SqlSchemaListOut(
        items=[
            SqlSchemaSummaryOut(id=item.id, name=item.name, updated_at=item.updated_at)
            for item in schemas
        ]
    )


@router.post("/sql/schemas", status_code=201)
async def create_schema(
    body: SqlSchemaRequest, user: Employee, container: ContainerDep
) -> SqlSchemaOut:
    """Сохранить схему базы."""
    return SqlSchemaOut.of(await container.sql_schemas.create(user.id, body.name, body.content))


@router.get("/sql/schemas/{id}")
async def get_schema(target: SchemaId, user: Employee, container: ContainerDep) -> SqlSchemaOut:
    """Схема пользователя с текстом."""
    return SqlSchemaOut.of(await container.sql_schemas.get(user.id, target))


@router.put("/sql/schemas/{id}")
async def update_schema(
    body: SqlSchemaRequest, target: SchemaId, user: Employee, container: ContainerDep
) -> SqlSchemaOut:
    """Заменить название и текст схемы."""
    schema = await container.sql_schemas.update(user.id, target, body.name, body.content)
    return SqlSchemaOut.of(schema)


@router.delete("/sql/schemas/{id}", status_code=204)
async def delete_schema(target: SchemaId, user: Employee, container: ContainerDep) -> Response:
    """Удалить схему."""
    await container.sql_schemas.delete(user.id, target)
    return Response(status_code=204)


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        yield chunk


@router.post("/docparse")
async def start_docparse(
    request: Request, user: Employee, session: ReadySession, container: ContainerDep
) -> StreamingResponse:
    """Запустить разбор документа; ответ — поток событий (§6.4)."""
    async with request.form(max_files=1, max_fields=2) as form:
        file = form.get("file")
        template_id = form.get("template_id")
        fields = []
        if not isinstance(file, UploadFile):
            fields.append(field_error("file", "required"))
        if not isinstance(template_id, str) or not template_id:
            fields.append(field_error("template_id", "required"))
        if fields or not isinstance(file, UploadFile) or not isinstance(template_id, str):
            raise validation_error(fields)
        channel = await container.docparse.start(
            user, session.session_id, template_id, file.filename or "", _chunks(file)
        )
    return event_stream_response(channel, container.settings.dialogs.keepalive_seconds)
