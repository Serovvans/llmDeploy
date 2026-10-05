"""Маршруты администрирования учётных записей (docs/portal-api.md §3)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from portal.auth.schemas import (
    AdminUserOut,
    AdminUserPageOut,
    AdminUserWithPasswordOut,
    CreateUserRequest,
    LoginUnlockOut,
    UpdateUserRequest,
)
from portal.core.access import Admin, ContainerDep, PageQueryDep
from portal.core.errors import field_error, not_found, validation_error

router = APIRouter(prefix="/api/admin/users")


def user_id(id: str, _: Admin) -> UUID:
    """Идентификатор из пути; не UUID — такого пользователя нет.

    Зависит от `Admin`: сессия и роль проверяются раньше ресурса (§1.2).
    """
    try:
        return UUID(id)
    except ValueError:
        raise not_found() from None


UserId = Annotated[UUID, Depends(user_id)]


@router.get("")
async def list_users(
    admin: Admin, container: ContainerDep, query: PageQueryDep
) -> AdminUserPageOut:
    """Страница учётных записей."""
    page = await container.admin.list_users(query)
    return AdminUserPageOut(
        items=[AdminUserOut.of(user, admin.id) for user in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
    )


@router.post("", status_code=201)
async def create_user(
    body: CreateUserRequest, admin: Admin, container: ContainerDep
) -> AdminUserWithPasswordOut:
    """Создать учётную запись и один раз показать временный пароль."""
    user, password = await container.admin.create_user(admin, body.full_name, body.login, body.role)
    return AdminUserWithPasswordOut(
        user=AdminUserOut.of(user, admin.id), temporary_password=password
    )


@router.patch("/{id}")
async def update_user(
    body: UpdateUserRequest, target: UserId, admin: Admin, container: ContainerDep
) -> AdminUserOut:
    """Изменить ФИО и (или) роль другого пользователя."""
    if body.full_name is None and body.role is None:
        raise validation_error([field_error("full_name", "required")])
    user = await container.admin.update_user(admin, target, body.full_name, body.role)
    return AdminUserOut.of(user, admin.id)


@router.post("/{id}/reset-password")
async def reset_password(
    target: UserId, admin: Admin, container: ContainerDep
) -> AdminUserWithPasswordOut:
    """Выдать новый временный пароль."""
    user, password = await container.admin.reset_password(admin, target)
    return AdminUserWithPasswordOut(
        user=AdminUserOut.of(user, admin.id), temporary_password=password
    )


@router.post("/{id}/reset-second-factor")
async def reset_second_factor(
    target: UserId, admin: Admin, container: ContainerDep
) -> AdminUserOut:
    """Сбросить второй фактор."""
    user = await container.admin.reset_second_factor(admin, target)
    return AdminUserOut.of(user, admin.id)


@router.post("/{id}/unlock-login")
async def unlock_login(target: UserId, admin: Admin, container: ContainerDep) -> LoginUnlockOut:
    """Снять временную блокировку входа; пароль, второй фактор и сессии не меняются."""
    user, unlocked = await container.admin.unlock_login(admin, target)
    return LoginUnlockOut(user=AdminUserOut.of(user, admin.id), unlocked=unlocked)


@router.post("/{id}/block")
async def block_user(target: UserId, admin: Admin, container: ContainerDep) -> AdminUserOut:
    """Заблокировать пользователя; его сессии гасятся."""
    user = await container.admin.set_blocked(admin, target, blocked=True)
    return AdminUserOut.of(user, admin.id)


@router.post("/{id}/unblock")
async def unblock_user(target: UserId, admin: Admin, container: ContainerDep) -> AdminUserOut:
    """Разблокировать пользователя."""
    user = await container.admin.set_blocked(admin, target, blocked=False)
    return AdminUserOut.of(user, admin.id)
