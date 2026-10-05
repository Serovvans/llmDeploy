"""Сборка приложения FastAPI: маршруты, общий формат ошибок, служебная проверка."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from portal.core.middleware import BODY_REFUSAL_KEY, BodyLimit, PortalMiddleware, error_response
from portal.core.validation import validation_failure
from portal.dialogs.routes import ATTACHMENT_UPLOAD_PATH
from portal.dialogs.routes import router as dialogs_router
from portal.kb.routes import DOCUMENT_UPLOAD_PATH
from portal.kb.routes import router as kb_router
from portal.tools.routes import DOCPARSE_UPLOAD_PATH
from portal.tools.routes import router as tools_router

_SESSION_ENDED_CODES = frozenset({"unauthenticated", "login_step_expired"})


async def _on_app_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, errors.AppError)
    response = error_response(error)
    if error.code in _SESSION_ENDED_CODES and SESSION_COOKIE in request.cookies:
        clear_session_cookie(response)
    return response


async def _on_validation_error(_: Request, error: Exception) -> Response:
    assert isinstance(error, RequestValidationError)
    return error_response(validation_failure(error.errors(), location_prefix=1))


async def _on_http_error(request: Request, error: Exception) -> Response:
    """Отказы фреймворка и приёма тела: неизвестный путь или метод — `not_found`."""
    assert isinstance(error, HTTPException)
    if error.status_code in (404, 405):
        return error_response(errors.not_found())
    if error.status_code == 413:
        refusal: errors.AppError = request.scope[BODY_REFUSAL_KEY]
        return error_response(refusal)
    return error_response(errors.bad_request())


def _body_limits(container: Container) -> tuple[BodyLimit, list[tuple[re.Pattern[str], BodyLimit]]]:
    """Пределы тела запроса (§1.3): общий и свои у маршрутов загрузки файла."""
    server = container.settings.server
    attachment_limit = container.settings.chat.attachment_max_bytes
    document_limit = container.settings.kb.document_max_bytes
    docparse_limit = container.settings.docparse.document_max_bytes
    default = BodyLimit(
        server.json_body_max_bytes, errors.request_too_large(server.json_body_max_bytes)
    )
    uploads = [
        (
            ATTACHMENT_UPLOAD_PATH,
            BodyLimit(
                attachment_limit + server.multipart_overhead_bytes,
                errors.file_too_large(attachment_limit),
            ),
        ),
        (
            DOCUMENT_UPLOAD_PATH,
            BodyLimit(
                document_limit + server.multipart_overhead_bytes,
                errors.file_too_large(document_limit),
            ),
        ),
        (
            DOCPARSE_UPLOAD_PATH,
            BodyLimit(
                docparse_limit + server.multipart_overhead_bytes,
                errors.file_too_large(docparse_limit),
            ),
        ),
    ]
    return default, uploads


def create_app(container: Container) -> FastAPI:
    """Создать приложение; документация FastAPI отключена во всех режимах (§1.1)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Процесс один (§13.5): всё, что осталось «формируется», никем не формируется.
        await container.dialogs.reset_interrupted()
        await container.dialogs.purge_empty()
        yield
        await container.generation.shutdown()
        await container.docparse.shutdown()
        container.sql_checker.close()
        container.hash_executor.shutdown()
        # Идущее чтение не ждём: тяжёлая страница PDF задержала бы остановку.
        container.document_executor.shutdown(wait=False, cancel_futures=True)
        await container.http_client.aclose()
        await container.qdrant.close()
        await container.engine.dispose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.container = container
    default_limit, upload_limits = _body_limits(container)
    app.add_middleware(PortalMiddleware, default_limit=default_limit, upload_limits=upload_limits)
    app.add_exception_handler(errors.AppError, _on_app_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(HTTPException, _on_http_error)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(config_router)
    app.include_router(dialogs_router)
    app.include_router(tools_router)
    app.include_router(kb_router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Процесс жив и база отвечает; только для healthcheck контейнера."""
        await ping(container.engine)
        return JSONResponse({"status": "ok"})

    return app
