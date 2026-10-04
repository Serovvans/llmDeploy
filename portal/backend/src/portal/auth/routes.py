"""Маршруты входа и сессий (docs/portal-api.md §2.4)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from portal.auth.domain import SessionState
from portal.auth.schemas import (
    ConfirmOut,
    ConfirmRequest,
    LoginRequest,
    PasswordRequest,
    QrOut,
    SecondFactorOut,
    SecondFactorRequest,
    SecondFactorSetupOut,
    SessionOut,
)
from portal.core.access import (
    SESSION_COOKIE,
    AnySession,
    ClientIp,
    ContainerDep,
    clear_session_cookie,
    session_on,
    set_session_cookie,
)
from portal.core.errors import field_error, validation_error
from portal.core.ports import SessionInfo

router = APIRouter(prefix="/api/auth")

SecondFactorStep = Annotated[SessionInfo, Depends(session_on("second_factor"))]
PasswordStep = Annotated[SessionInfo, Depends(session_on("password_change", "ready"))]
SetupStep = Annotated[SessionInfo, Depends(session_on("second_factor_setup"))]


def _refresh_cookie(
    request: Request, response: Response, container: ContainerDep, state: SessionState
) -> None:
    """Перевыставить cookie: значение прежнее или новое, срок — до жёсткого срока сессии."""
    token = state.new_token or request.cookies[SESSION_COOKIE]
    remaining = state.expires_at - container.clock.now()
    set_session_cookie(response, token, max(0, int(remaining.total_seconds())))


@router.post("/login")
async def login(
    body: LoginRequest, request: Request, response: Response, container: ContainerDep, ip: ClientIp
) -> SessionOut:
    """Вход по логину и паролю; дальше — шаг из ответа."""
    state = await container.auth.login(body.login, body.password, ip)
    _refresh_cookie(request, response, container, state)
    return SessionOut.of(state)


@router.get("/session")
async def get_session(info: AnySession, container: ContainerDep) -> SessionOut:
    """Текущий шаг входа и данные пользователя."""
    return SessionOut.of(await container.auth.session_state(info.session_id))


@router.post("/second-factor")
async def second_factor(
    body: SecondFactorRequest,
    info: SecondFactorStep,
    request: Request,
    response: Response,
    container: ContainerDep,
    ip: ClientIp,
) -> SecondFactorOut:
    """Код из приложения или резервный код."""
    if body.code is None and body.backup_code is None:
        raise validation_error([field_error("code", "required")])
    if body.code is not None and body.backup_code is not None:
        raise validation_error([field_error("backup_code", "invalid_format")])
    result = await container.auth.second_factor(info.session_id, body.code, body.backup_code, ip)
    _refresh_cookie(request, response, container, result.state)
    return SecondFactorOut(
        session=SessionOut.of(result.state), backup_code_used=result.backup_code_used
    )


@router.post("/password", response_model=None)
async def change_password(
    body: PasswordRequest,
    info: PasswordStep,
    request: Request,
    response: Response,
    container: ContainerDep,
    ip: ClientIp,
) -> SessionOut | Response:
    """Смена пароля: обязательная выдаёт новую сессию, добровольная завершает все."""
    state = await container.auth.change_password(
        info.session_id, body.new_password, body.current_password, ip
    )
    if state is None:
        finished = Response(status_code=204)
        clear_session_cookie(finished)
        return finished
    _refresh_cookie(request, response, container, state)
    return SessionOut.of(state)


@router.post("/second-factor/setup")
async def start_setup(info: SetupStep, container: ContainerDep) -> SecondFactorSetupOut:
    """Ключ и QR-код для приложения-аутентификатора."""
    setup = await container.auth.start_second_factor_setup(info.session_id)
    return SecondFactorSetupOut(
        secret=setup.secret, qr=QrOut(size=setup.qr_size, path=setup.qr_path)
    )


@router.post("/second-factor/confirm")
async def confirm_setup(
    body: ConfirmRequest,
    info: SetupStep,
    request: Request,
    response: Response,
    container: ContainerDep,
    ip: ClientIp,
) -> ConfirmOut:
    """Подтверждение настройки кодом; резервные коды отдаются один раз."""
    result = await container.auth.confirm_second_factor_setup(info.session_id, body.code, ip)
    _refresh_cookie(request, response, container, result.state)
    return ConfirmOut(session=SessionOut.of(result.state), backup_codes=result.backup_codes)


@router.post("/logout", status_code=204)
async def logout(info: AnySession, container: ContainerDep) -> Response:
    """Выход: сессия удаляется."""
    await container.auth.logout(info.session_id)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response
