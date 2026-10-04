"""Сборка приложения FastAPI: маршруты, общий формат ошибок, служебная проверка."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from portal.auth.admin_routes import router as admin_router
from portal.auth.routes import router as auth_router
from portal.core import errors
from portal.core.access import SESSION_COOKIE, clear_session_cookie
from portal.core.config_routes import router as config_router
from portal.core.container import Container
from portal.core.db import ping
from portal.core.middleware import PortalMiddleware, error_response

_SESSION_ENDED_CODES = frozenset({"unauthenticated", "login_step_expired"})
_REQUIRED_TYPES = frozenset({"missing", "string_too_short", "too_short"})
_TOO_LONG_TYPES = frozenset({"string_too_long", "too_long"})
_UNKNOWN_VALUE_TYPES = frozenset({"literal_error", "enum"})


def _field_code(error_type: str) -> str:
    if error_type in _REQUIRED_TYPES:
        return "required"
    if error_type in _TOO_LONG_TYPES:
        return "too_long"
    if error_type in _UNKNOWN_VALUE_TYPES:
        return "unknown_value"
    return "invalid_format"


def _validation_failure(problems: Sequence[dict[str, Any]]) -> errors.AppError:
    """Перевести ошибки разбора запроса в общий формат: по одной записи на поле."""
    fields: dict[str, errors.FieldError] = {}
    for problem in problems:
        location = problem["loc"]
        if problem["type"] == "json_invalid" or len(location) < 2:
            return errors.bad_request()
        name = ".".join(str(part) for part in location[1:])
        fields.setdefault(name, errors.field_error(name, _field_code(problem["type"])))
    return errors.validation_error(list(fields.values()))


async def _on_app_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, errors.AppError)
    response = error_response(error)
    if error.code in _SESSION_ENDED_CODES and SESSION_COOKIE in request.cookies:
        clear_session_cookie(response)
    return response


async def _on_validation_error(_: Request, error: Exception) -> Response:
    assert isinstance(error, RequestValidationError)
    return error_response(_validation_failure(error.errors()))


async def _on_http_error(request: Request, error: Exception) -> Response:
    """Отказы фреймворка и приёма тела: неизвестный путь или метод — `not_found`."""
    assert isinstance(error, HTTPException)
    if error.status_code in (404, 405):
        return error_response(errors.not_found())
    if error.status_code == 413:
        limit = request.app.state.container.settings.server.json_body_max_bytes
        return error_response(errors.request_too_large(limit))
    return error_response(errors.bad_request())


def create_app(container: Container) -> FastAPI:
    """Создать приложение; документация FastAPI отключена во всех режимах (§1.1)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await container.engine.dispose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.container = container
    app.add_middleware(
        PortalMiddleware, json_body_max_bytes=container.settings.server.json_body_max_bytes
    )
    app.add_exception_handler(errors.AppError, _on_app_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(HTTPException, _on_http_error)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(config_router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Процесс жив и база отвечает; только для healthcheck контейнера."""
        await ping(container.engine)
        return JSONResponse({"status": "ok"})

    return app
