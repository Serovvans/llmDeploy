"""Клиент песочницы сборки `code-sandbox` (docs/portal-api.md §7.5.7, §7.5.12).

Очередь проекта и места — в памяти процесса: `portal-api` один (§13.5), и после его
перезапуска незаконченной сборки нет. В журнал идут только статус и код отказа, без
путей, кода и сообщений компилятора.
"""

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import UUID

import httpx

from portal.code.ports import (
    BuildInProgressError,
    BuildRules,
    BuildUnavailableError,
    CodeSandboxSlot,
    CompileFile,
    CompileResult,
    Diagnostic,
    RuleFinding,
    RuleTool,
)
from portal.core.settings import CodeBuildSettings

logger = logging.getLogger(__name__)

_STATUSES = frozenset({"succeeded", "failed", "timeout"})
_SEVERITIES = frozenset({"error", "warning"})
_TIMEOUT_RESULT = CompileResult("timeout", None, (), False, None, None, (), ())


class _ProtocolError(Exception):
    """Ответ песочницы не по протоколу §7.5.12."""


def _text(value: Any, *, optional: bool = False) -> Any:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _ProtocolError
    return value


def _number(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ProtocolError
    return value


def _bytes(value: Any) -> bytes:
    try:
        return base64.b64decode(_text(value), validate=True)
    except binascii.Error as error:
        raise _ProtocolError from error


def _diagnostic(item: dict[str, Any]) -> Diagnostic:
    if item.get("severity") not in _SEVERITIES:
        raise _ProtocolError
    return Diagnostic(
        severity=item["severity"],
        code=_text(item.get("code")),
        file=_text(item.get("file"), optional=True),
        line=_number(item.get("line")),
        column=_number(item.get("column")),
        end_line=_number(item.get("end_line")),
        end_column=_number(item.get("end_column")),
        message=_text(item.get("message")),
    )


def _rules(value: Any) -> BuildRules:
    if not isinstance(value, dict):
        raise _ProtocolError
    return BuildRules(
        factory=_text(value.get("factory"), optional=True),
        tools=[
            RuleTool(
                _text(item.get("name")),
                _text(item.get("title"), optional=True),
                _text(item.get("category"), optional=True),
            )
            for item in _dicts(value.get("tools"))
        ],
        findings=[
            RuleFinding(
                rule=_text(item.get("rule")),
                file=_text(item.get("file"), optional=True),
                line=_number(item.get("line")),
                column=_number(item.get("column")),
                message=_text(item.get("message")),
            )
            for item in _dicts(value.get("findings"))
        ],
    )


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise _ProtocolError
    return value


def parse_result(body: Any) -> CompileResult:
    """Ответ `200` песочницы → `CompileResult`; не по протоколу — `_ProtocolError`."""
    if not isinstance(body, dict) or body.get("status") not in _STATUSES:
        raise _ProtocolError
    on_server = body.get("on_server")
    if not isinstance(on_server, list) or not isinstance(body.get("diagnostics_truncated"), bool):
        raise _ProtocolError
    succeeded = body["status"] == "succeeded"
    return CompileResult(
        status=body["status"],
        sdk_version=_text(body.get("sdk_version"), optional=True),
        diagnostics=[_diagnostic(item) for item in _dicts(body.get("diagnostics"))],
        diagnostics_truncated=body["diagnostics_truncated"],
        rules=_rules(body.get("rules")) if succeeded else None,
        dll=_bytes(body.get("dll")) if succeeded else None,
        bundle=[
            (_text(item.get("file_name")), _bytes(item.get("content")))
            for item in _dicts(body.get("bundle"))
        ],
        on_server=[_text(item) for item in on_server],
    )


class _HttpSlot:
    """Занятое место: один запрос `POST /compile`."""

    def __init__(self, client: httpx.AsyncClient, settings: CodeBuildSettings) -> None:
        self._client = client
        self._settings = settings

    async def compile(
        self,
        assembly_name: str,
        files: Sequence[CompileFile],
        packages: Sequence[tuple[str, str]],
    ) -> CompileResult:
        """Собрать проект в песочнице; общий срок — `code.build.timeout_seconds`."""
        payload = {
            "assembly_name": assembly_name,
            "files": [{"path": file.path, "content": file.content} for file in files],
            "packages": [
                {"id": package_id, "version": version} for package_id, version in packages
            ],
        }
        url = f"{self._settings.url.rstrip('/')}/compile"
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                return await self._send(url, payload)
        except (TimeoutError, httpx.TimeoutException):
            return _TIMEOUT_RESULT

    async def _send(self, url: str, payload: dict[str, Any]) -> CompileResult:
        limit = self._settings.response_max_bytes
        try:
            async with self._client.stream(
                "POST", url, json=payload, timeout=self._settings.timeout_seconds
            ) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        logger.warning("code sandbox refused", extra={"reason": "too_large"})
                        raise BuildUnavailableError
                    chunks.append(chunk)
                status = response.status_code
        except httpx.TimeoutException:
            raise  # срок — итог `timeout`, а не отказ (§7.5.7, шаг 5)
        except httpx.HTTPError as error:
            logger.warning(
                "code sandbox refused",
                extra={"reason": "connection", "error_type": type(error).__name__},
            )
            raise BuildUnavailableError from error
        body = b"".join(chunks)
        if status != httpx.codes.OK:
            logger.warning(
                "code sandbox refused", extra={"status": status, "error": _error_code(body)}
            )
            raise BuildUnavailableError
        try:
            return parse_result(json.loads(body))
        except (ValueError, _ProtocolError) as error:
            logger.warning("code sandbox refused", extra={"reason": "protocol"})
            raise BuildUnavailableError from error


def _error_code(body: bytes) -> str | None:
    """Код отказа песочницы (`busy`, `sdk_missing`, …) — только из известного набора."""
    try:
        value = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return None
    known = {"invalid_request", "unknown_package", "busy", "sdk_missing", "internal"}
    return value if value in known else None


class HttpCodeSandbox:
    """Реализация порта `CodeSandbox` по HTTP (§7.5.12)."""

    def __init__(self, client: httpx.AsyncClient, settings: CodeBuildSettings) -> None:
        """Получить клиент HTTP и настройки сборки."""
        self._client = client
        self._settings = settings
        self._places = asyncio.Semaphore(settings.max_parallel)
        self._queues: dict[UUID, asyncio.Lock] = {}
        self._buttons: set[UUID] = set()
        self._running: dict[UUID, int] = {}

    def running(self, project_id: UUID) -> bool:
        """Идёт ли сборка проекта, включая ожидание очереди и места."""
        return self._running.get(project_id, 0) > 0

    @asynccontextmanager
    async def slot(self, project_id: UUID, *, button: bool) -> AsyncIterator[CodeSandboxSlot]:
        """Очередь проекта, затем место на портале; всё ожидание — до `wait_seconds`."""
        if button and project_id in self._buttons:
            raise BuildInProgressError
        if button:
            self._buttons.add(project_id)
        self._running[project_id] = self._running.get(project_id, 0) + 1
        queue = self._queues.setdefault(project_id, asyncio.Lock())
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(self._settings.wait_seconds):
                        await stack.enter_async_context(queue)
                        await stack.enter_async_context(self._places)
                except TimeoutError as error:
                    raise BuildUnavailableError from error
                yield _HttpSlot(self._client, self._settings)
        finally:
            self._running[project_id] -= 1
            if not self._running[project_id]:
                del self._running[project_id]
                if not queue.locked():
                    self._queues.pop(project_id, None)
            if button:
                self._buttons.discard(project_id)
