"""Проверка сессии, шага входа и роли для маршрутов (docs/portal-api.md §1.2, §2.5)."""

import ipaddress
from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import Depends, Request, Response

from portal.core import errors
from portal.core.container import Container
from portal.core.ports import CurrentUser, LoginStep, SessionInfo, SessionMissingError

SESSION_COOKIE = "portal_session"


def get_container(request: Request) -> Container:
    """Собранные зависимости приложения."""
    return cast(Container, request.app.state.container)


def client_ip(request: Request) -> str | None:
    """Адрес клиента: последний элемент `X-Forwarded-For` (его дописывает Caddy).

    Значения левее (и в строках заголовка выше) присланы клиентом и игнорируются; без
    заголовка — адрес соединения.
    """
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    candidate = forwarded.rsplit(",", 1)[-1].strip() if forwarded else None
    if candidate is None and request.client is not None:
        candidate = request.client.host
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else None
    except ValueError:
        return None


def set_session_cookie(response: Response, token: str, max_age: int) -> None:
    """Выставить cookie сессии; атрибуты не настраиваются (docs/portal-api.md §2.1)."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/api",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    """Удалить cookie сессии у клиента."""
    set_session_cookie(response, "", 0)


ContainerDep = Annotated[Container, Depends(get_container)]
ClientIp = Annotated[str | None, Depends(client_ip)]


def session_on(*steps: LoginStep) -> Callable[[Request, Container], Awaitable[SessionInfo]]:
    """Зависимость: действующая сессия на одном из шагов (без шагов — на любом).

    Пропавшая сессия у маршрута шага входа даёт `login_step_expired` вместо
    `unauthenticated` (§2.4). Маршрут, доступный и на шаге `ready`, отвечает так, только
    если истёкшая сессия была на его незавершённом шаге.
    """

    async def dependency(request: Request, container: ContainerDep) -> SessionInfo:
        try:
            info = await container.authenticator.authenticate(request.cookies.get(SESSION_COOKIE))
        except SessionMissingError as missing:
            on_login_step = missing.step in steps and missing.step != "ready"
            if steps and (on_login_step or "ready" not in steps):
                raise errors.login_step_expired() from missing
            raise errors.unauthenticated() from missing
        if steps and info.step not in steps:
            raise errors.login_step_required(info.step)
        return info

    return dependency


AnySession = Annotated[SessionInfo, Depends(session_on())]


ReadySession = Annotated[SessionInfo, Depends(session_on("ready"))]


async def current_user(info: ReadySession, ip: ClientIp) -> CurrentUser:
    """Зависимость уровня «сотрудник»: вход завершён, роль любая."""
    return CurrentUser(id=info.user_id, role=info.role, full_name=info.full_name, ip=ip)


Employee = Annotated[CurrentUser, Depends(current_user)]


async def current_admin(user: Employee) -> CurrentUser:
    """Зависимость уровня «администратор»."""
    if user.role != "admin":
        raise errors.forbidden()
    return user


Admin = Annotated[CurrentUser, Depends(current_admin)]
