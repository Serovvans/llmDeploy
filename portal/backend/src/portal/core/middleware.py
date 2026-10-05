"""Обёртка всех запросов: защита от CSRF, запрет кэширования, сбои, журнал запросов."""

import logging
import re
import time
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from portal.core import errors
from portal.core.logging import code_locations

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD"})
_API_PREFIX = "/api/"
_UNAVAILABLE = (OperationalError, InterfaceError, OSError, TimeoutError)
BODY_REFUSAL_KEY = "portal.body_refusal"


@dataclass(frozen=True)
class BodyLimit:
    """Предел размера тела запроса и отказ, которым отвечает его превышение."""

    max_bytes: int
    refusal: errors.AppError


def error_body(error: errors.AppError) -> dict[str, Any]:
    """Тело ответа в общем формате ошибки (docs/portal-api.md §1.3)."""
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.fields:
        body["fields"] = [
            {"field": item.field, "code": item.code, "message": item.message}
            for item in error.fields
        ]
    if error.details is not None:
        body["details"] = error.details
    return {"error": body}


def error_response(error: errors.AppError) -> JSONResponse:
    """Ответ с ошибкой; у отказов с ожиданием — заголовок `Retry-After`."""
    response = JSONResponse(error_body(error), status_code=error.status)
    retry_after = (error.details or {}).get("retry_after_seconds")
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response


class PortalMiddleware:
    """Чистое ASGI-промежуточное звено: не буферизует ответ и не мешает потокам событий."""

    def __init__(
        self,
        app: ASGIApp,
        default_limit: BodyLimit,
        upload_limits: Sequence[tuple[re.Pattern[str], BodyLimit]],
    ) -> None:
        """Обернуть приложение.

        `default_limit` — предел тела запроса без файла; `upload_limits` — свои пределы
        маршрутов загрузки файла по шаблону пути (§1.3).
        """
        self._app = app
        self._default_limit = default_limit
        self._upload_limits = upload_limits

    def _limit_for(self, scope: Scope) -> BodyLimit:
        if scope["method"] == "POST":
            for pattern, limit in self._upload_limits:
                if pattern.fullmatch(scope["path"]):
                    return limit
        return self._default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Обработать запрос."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = time.monotonic()
        is_api = scope["path"].startswith(_API_PREFIX)
        status: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                if is_api:
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        try:
            if not is_api:
                await self._app(scope, receive, send_wrapper)
            elif scope["method"] not in _SAFE_METHODS and not _has_csrf_header(scope):
                await error_response(errors.csrf_check_failed())(scope, receive, send_wrapper)
            elif _declared_length(scope) > (limit := self._limit_for(scope)).max_bytes:
                await error_response(limit.refusal)(scope, receive, send_wrapper)
            else:
                scope[BODY_REFUSAL_KEY] = limit.refusal
                await self._app(scope, _limited(receive, limit.max_bytes), send_wrapper)
        except Exception as error:
            if "code" in status:
                raise
            unavailable = isinstance(error, _UNAVAILABLE)
            # В журнал идут только тип исключения и места в коде: в тексте исключения и
            # в стандартной трассировке (она его включает) могут оказаться данные запроса.
            logger.error(
                "request failed",
                extra={
                    "path": scope["path"],
                    "error_type": type(error).__name__,
                    "trace": code_locations(error),
                },
            )
            failure = errors.service_unavailable() if unavailable else errors.internal_error()
            await error_response(failure)(scope, receive, send_wrapper)
        finally:
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status.get("code"),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )


def _limited(receive: Receive, max_bytes: int) -> Receive:
    """Приём тела с подсчётом байтов: на пределе чтение обрывается отказом 413.

    Отказ поднимается как `HTTPException`: только его фреймворк пропускает из разбора
    тела без подмены; в общий формат его переводит обработчик приложения, который берёт
    готовый отказ из `scope[BODY_REFUSAL_KEY]`.
    """
    received = 0

    async def limited() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > max_bytes:
                raise HTTPException(status_code=413)
        return message

    return limited


def _declared_length(scope: Scope) -> int:
    """Значение `Content-Length`; 0 — заголовка нет или он не число."""
    for name, value in scope["headers"]:
        if name == b"content-length" and value.isdigit():
            return int(value)
    return 0


def _has_csrf_header(scope: MutableMapping[str, Any]) -> bool:
    return any(name == b"x-portal-csrf" and value == b"1" for name, value in scope["headers"])
