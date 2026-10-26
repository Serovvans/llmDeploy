"""Маршруты подключений к базам заказчика (docs/portal-api.md §7.6.2–7.6.3).

Сначала сессия и роль, затем флаг `db.enabled` (`403 feature_disabled`), затем ресурс.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import TypeAdapter, ValidationError

from portal.core.access import Admin, ContainerDep, Employee
from portal.core.errors import feature_disabled, not_found
from portal.core.validation import validation_failure
from portal.databases.schemas import (
    AdminDbConnectionListOut,
    AdminDbConnectionOut,
    ConnectionRequest,
    DbConnectionListOut,
    DbConnectionOut,
    DbSnapshotOut,
)

admin_router = APIRouter(prefix="/api/admin/db-connections")
router = APIRouter(prefix="/api/db/connections")


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise not_found() from None


def admin_enabled(_: Admin, container: ContainerDep) -> None:
    """Флаг выключен — `403 feature_disabled` (§7.6.10)."""
    if not container.settings.db.enabled:
        raise feature_disabled()


def employee_enabled(_: Employee, container: ContainerDep) -> None:
    """Флаг выключен — `403 feature_disabled` (§7.6.10)."""
    if not container.settings.db.enabled:
        raise feature_disabled()


AdminEnabled = Annotated[None, Depends(admin_enabled)]
EmployeeEnabled = Annotated[None, Depends(employee_enabled)]


def admin_connection_id(id: str, _: AdminEnabled) -> UUID:
    """Идентификатор подключения из пути; роль и флаг проверяются раньше ресурса."""
    return _uuid(id)


def connection_id(id: str, _: EmployeeEnabled) -> UUID:
    """Идентификатор подключения из пути сотрудника."""
    return _uuid(id)


AdminConnectionId = Annotated[UUID, Depends(admin_connection_id)]
ConnectionId = Annotated[UUID, Depends(connection_id)]


def _request(body: dict[str, Any]) -> ConnectionRequest:
    try:
        return TypeAdapter(ConnectionRequest).validate_python(body)
    except ValidationError as error:
        raise validation_failure(error.errors()) from None


@admin_router.get("")
async def list_connections(_: AdminEnabled, container: ContainerDep) -> AdminDbConnectionListOut:
    """Все подключения по названию."""
    views = await container.databases.admin_list()
    return AdminDbConnectionListOut(items=[AdminDbConnectionOut.of(view) for view in views])


@admin_router.post("", status_code=201)
async def create_connection(
    body: dict[str, Any], _: AdminEnabled, admin: Admin, container: ContainerDep
) -> AdminDbConnectionOut:
    """Новое подключение; пароль в ответе не возвращается."""
    view = await container.databases.create(admin, _request(body).to_input())
    return AdminDbConnectionOut.of(view)


@admin_router.patch("/{id}")
async def update_connection(
    body: dict[str, Any], target: AdminConnectionId, admin: Admin, container: ContainerDep
) -> AdminDbConnectionOut:
    """Изменить поля подключения; хотя бы одно."""
    view = await container.databases.update(admin, target, _request(body).to_input())
    return AdminDbConnectionOut.of(view)


@admin_router.delete("/{id}", status_code=204)
async def delete_connection(
    target: AdminConnectionId, admin: Admin, container: ContainerDep
) -> Response:
    """Удалить подключение со списком доступа и снимком."""
    await container.databases.delete(admin, target)
    return Response(status_code=204)


@admin_router.post("/{id}/check")
async def check_connection(
    target: AdminConnectionId, admin: Admin, container: ContainerDep
) -> AdminDbConnectionOut:
    """«Проверить подключение»: права и схемы; неудача соединения — в `last_error`."""
    return AdminDbConnectionOut.of(await container.databases.check(admin, target))


@router.get("")
async def list_available(
    _: EmployeeEnabled, user: Employee, container: ContainerDep
) -> DbConnectionListOut:
    """Доступные сотруднику подключения."""
    views = await container.databases.employee_list(user)
    return DbConnectionListOut(items=[DbConnectionOut.of(view) for view in views])


@router.get("/{id}/snapshot")
async def get_snapshot(
    target: ConnectionId, user: Employee, container: ContainerDep
) -> DbSnapshotOut:
    """Снимок схемы доступного подключения."""
    return DbSnapshotOut(**await container.databases.snapshot(user, target))


@router.post("/{id}/snapshot")
async def refresh_snapshot(
    target: ConnectionId, user: Employee, container: ContainerDep
) -> DbSnapshotOut:
    """«Обновить схему» — не чаще интервала."""
    return DbSnapshotOut(**await container.databases.refresh(user, target))
