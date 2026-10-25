"""Маршруты раздела «Код» (docs/portal-api.md §7.5.2); уровень «сотрудник».

Каждый маршрут сначала проверяет флаг `code.enabled` (`403 feature_disabled`), затем
проект владельца: чужой — `404`, как несуществующий.
"""

import re
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import UploadFile

from portal.code.schemas import (
    AddFileRequest,
    CodeBuildOut,
    CodeFileOut,
    CodeProjectItemOut,
    CodeProjectListOut,
    CodeProjectOut,
    CodeSnapshotListOut,
    CodeSnapshotOut,
    CreatedProjectOut,
    CreateProjectRequest,
    DecisionOut,
    DecisionRequest,
    PatchProjectRequest,
    UpdateFileRequest,
    WizardProjectRequest,
)
from portal.code.service import UNSET, Created
from portal.core.access import ContainerDep, Employee
from portal.core.errors import (
    feature_disabled,
    field_error,
    file_too_large,
    not_found,
    validation_error,
)
from portal.core.validation import validation_failure
from portal.dialogs.service import CarriedInput

router = APIRouter(prefix="/api/code")

# Маршрут загрузки архива: у него свой предел размера тела (§1.3, §7.5.5).
IMPORT_UPLOAD_PATH = re.compile(r"/api/code/projects/import")

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_ZIP = "application/zip"
_CREATE = TypeAdapter[Any](CreateProjectRequest)


def code_enabled(_: Employee, container: ContainerDep) -> None:
    """Раздел «Код» выключен флагом — все маршруты `403 feature_disabled` (§7.5.15)."""
    if not container.settings.code.enabled:
        raise feature_disabled()


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise not_found() from None


def project_id(id: str, _: None = Depends(code_enabled)) -> UUID:
    """Идентификатор проекта из пути; флаг и сессия проверяются раньше ресурса (§1.2)."""
    return _uuid(id)


def file_id(file_id: str, _: None = Depends(code_enabled)) -> UUID:
    """Идентификатор файла проекта из пути."""
    return _uuid(file_id)


def snapshot_id(snapshot_id: str, _: None = Depends(code_enabled)) -> UUID:
    """Идентификатор версии из пути."""
    return _uuid(snapshot_id)


def message_id(message_id: str, _: None = Depends(code_enabled)) -> UUID:
    """Идентификатор ответа с правкой из пути."""
    return _uuid(message_id)


ProjectId = Annotated[UUID, Depends(project_id)]
FileId = Annotated[UUID, Depends(file_id)]
SnapshotId = Annotated[UUID, Depends(snapshot_id)]
MessageId = Annotated[UUID, Depends(message_id)]
Enabled = Annotated[None, Depends(code_enabled)]


def build_enabled(_: Enabled, container: ContainerDep) -> None:
    """Сборка и архив выключены флагом `code.build.enabled` — `403 feature_disabled`."""
    if not container.settings.code.build.enabled:
        raise feature_disabled()


BuildEnabled = Annotated[None, Depends(build_enabled)]


def _zip(content: bytes, fallback: str, file_name: str) -> Response:
    """Файл ZIP с именем по §1.5: ASCII-запасное и настоящее в `filename*`."""
    return Response(
        content,
        media_type=_ZIP,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(file_name)}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _created(created: Created) -> CreatedProjectOut:
    return CreatedProjectOut(
        project=CodeProjectOut.of(created.details),
        assistant_request=created.assistant_request,
        import_=created.report,
    )


def _validated[T](model: type[T], body: dict[str, Any]) -> T:
    try:
        return TypeAdapter(model).validate_python(body)
    except ValidationError as error:
        raise validation_failure(error.errors()) from None


@router.get("/projects")
async def list_projects(
    _: Enabled,
    user: Employee,
    container: ContainerDep,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: str | None = None,
) -> CodeProjectListOut:
    """Проекты сотрудника, от недавно изменённых."""
    page = await container.code.list_projects(user.id, limit or 30, cursor)
    return CodeProjectListOut(
        items=[CodeProjectItemOut.of(item) for item in page.items], next_cursor=page.next_cursor
    )


@router.post("/projects", status_code=201)
async def create_project(
    body: dict[str, Any], _: Enabled, user: Employee, container: ContainerDep
) -> CreatedProjectOut:
    """Проект из мастера (с `carried` — перенос в «Код») или проект-пример (§7.5.4)."""
    try:
        request = _CREATE.validate_python(body)
    except ValidationError as error:
        raise validation_failure(error.errors()) from None
    if not isinstance(request, WizardProjectRequest):
        return _created(await container.code.create_example(user.id, request.name))
    carried = None
    if request.carried is not None:
        if not container.settings.memory.enabled:
            raise feature_disabled()  # флаг «Память диалога» выключен (§5.10.9)
        carried = CarriedInput(request.carried.text, request.carried.source_dialog_id)
    return _created(await container.code.create_wizard(user.id, request.wizard, carried))


async def _read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """Содержимое файла формы; больше `max_bytes` — `413 file_too_large`."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        size += len(chunk)
        if size > max_bytes:
            raise file_too_large(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/projects/import", status_code=201)
async def import_project(
    request: Request, _: Enabled, user: Employee, container: ContainerDep
) -> CreatedProjectOut:
    """Проект из архива ZIP (§7.5.5); сам архив не сохраняется."""
    max_bytes = container.settings.code.import_max_bytes
    async with request.form(max_files=1, max_fields=2) as form:
        file = form.get("file")
        name = form.get("name")
        fields = []
        if not isinstance(file, UploadFile):
            fields.append(field_error("file", "required"))
        if not isinstance(name, str) or not name:
            fields.append(field_error("name", "required"))
        if fields or not isinstance(file, UploadFile) or not isinstance(name, str):
            raise validation_error(fields)
        data = await _read_limited(file, max_bytes)
    return _created(await container.code.import_zip(user.id, name, data))


@router.get("/projects/{id}")
async def get_project(target: ProjectId, user: Employee, container: ContainerDep) -> CodeProjectOut:
    """Проект с файлами, последней сборкой и диалогом помощника."""
    return CodeProjectOut.of(await container.code.get(user.id, target))


@router.patch("/projects/{id}")
async def patch_project(
    body: dict[str, Any], target: ProjectId, user: Employee, container: ContainerDep
) -> CodeProjectOut:
    """Пакеты и схема проекта; хотя бы одно поле."""
    patch = _validated(PatchProjectRequest, body)
    if not patch.model_fields_set:
        raise validation_error([field_error("packages", "required")])
    schema_id = patch.schema_id if "schema_id" in patch.model_fields_set else UNSET
    details = await container.code.patch(user.id, target, patch.packages, schema_id)
    return CodeProjectOut.of(details)


@router.delete("/projects/{id}", status_code=204)
async def delete_project(target: ProjectId, user: Employee, container: ContainerDep) -> Response:
    """Удалить проект с файлами, версиями, сборкой и диалогом помощника."""
    await container.code.delete(user.id, target)
    return Response(status_code=204)


@router.post("/projects/{id}/files", status_code=201)
async def add_file(
    body: dict[str, Any], target: ProjectId, user: Employee, container: ContainerDep
) -> CodeFileOut:
    """Новый файл проекта."""
    request = _validated(AddFileRequest, body)
    return CodeFileOut.of(
        await container.code.add_file(user.id, target, request.path, request.content)
    )


@router.patch("/projects/{id}/files/{file_id}")
async def update_file(
    body: dict[str, Any],
    target: ProjectId,
    file: FileId,
    user: Employee,
    container: ContainerDep,
) -> CodeFileOut:
    """Изменить или переименовать файл с проверкой ревизии."""
    request = _validated(UpdateFileRequest, body)
    if request.path is None and request.content is None:
        raise validation_error([field_error("content", "required")])
    updated = await container.code.update_file(
        user.id, target, file, request.revision, request.path, request.content
    )
    return CodeFileOut.of(updated)


@router.delete("/projects/{id}/files/{file_id}", status_code=204)
async def delete_file(
    target: ProjectId, file: FileId, user: Employee, container: ContainerDep
) -> Response:
    """Удалить файл проекта."""
    await container.code.delete_file(user.id, target, file)
    return Response(status_code=204)


@router.get("/projects/{id}/export")
async def export_project(target: ProjectId, user: Employee, container: ContainerDep) -> Response:
    """Выгрузка проекта для Visual Studio (§7.5.5)."""
    name, content = await container.code.export(user.id, target)
    return _zip(content, "project.zip", f"{name}.zip")


@router.get("/projects/{id}/snapshots")
async def list_snapshots(
    target: ProjectId, user: Employee, container: ContainerDep
) -> CodeSnapshotListOut:
    """Хранимые версии, новые сверху."""
    snapshots = await container.code.snapshots(user.id, target)
    return CodeSnapshotListOut(items=[CodeSnapshotOut.of(item) for item in snapshots])


@router.post("/projects/{id}/snapshots/{snapshot_id}/restore")
async def restore_snapshot(
    target: ProjectId, snapshot: SnapshotId, user: Employee, container: ContainerDep
) -> CodeProjectOut:
    """Вернуть проект к версии (§7.5.6)."""
    return CodeProjectOut.of(await container.code.restore(user.id, target, snapshot))


@router.post("/projects/{id}/build")
async def build_project(
    target: ProjectId, _: BuildEnabled, user: Employee, container: ContainerDep
) -> CodeBuildOut:
    """Сборка кнопкой «Собрать» (§7.5.7)."""
    result = await container.code.build(user.id, target)
    return CodeBuildOut.of(result.build, result.outdated)


@router.get("/projects/{id}/build/archive")
async def build_archive(
    target: ProjectId, _: BuildEnabled, user: Employee, container: ContainerDep
) -> Response:
    """Архив для CoGIS последней успешной сборки (§7.5.7)."""
    name, content = await container.code.archive(user.id, target)
    return _zip(content, "project-cogis.zip", f"{name}-cogis.zip")


@router.get("/projects/{id}/edits/{message_id}")
async def get_edit(
    target: ProjectId, message: MessageId, user: Employee, container: ContainerDep
) -> dict[str, Any]:
    """Правка помощника с предлагаемым и текущим содержимым файлов (`CodeEditFull`)."""
    edit = await container.code.edit(user.id, target, message)
    return {**edit.summary, "files": edit.files}


@router.post("/projects/{id}/edits/{message_id}/decision")
async def decide_edit(
    body: dict[str, Any],
    target: ProjectId,
    message: MessageId,
    user: Employee,
    container: ContainerDep,
) -> DecisionOut:
    """Решение по правке: принять все, часть файлов или ничего (§7.5.10)."""
    request = _validated(DecisionRequest, body)
    accept = [(item.path, item.revision) for item in request.accept]
    edit, details = await container.code.decide(user.id, target, message, accept)
    return DecisionOut(edit=edit, project=CodeProjectOut.of(details))


def _etag_matches(header: str | None, etag: str) -> bool:
    """`If-None-Match` совпал с `ETag`: список значений, слабое сравнение (RFC 9110 §13.1.2)."""
    if header is None:
        return False
    values = {item.strip().removeprefix("W/") for item in header.split(",")}
    return "*" in values or etag in values


@router.get("/sdk")
async def get_sdk(request: Request, _: Enabled, container: ContainerDep) -> Response:
    """Справочник SDK для подсказок редактора; кеш браузера по `ETag` (§7.5.14)."""
    sdk = await container.code.sdk()
    headers = {"ETag": sdk.etag, "Cache-Control": "private, no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), sdk.etag):
        return Response(status_code=304, headers=headers)
    return Response(sdk.body, media_type="application/json", headers=headers)
