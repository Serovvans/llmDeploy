"""Помощники тестов раздела «Код»: настройки с включённым разделом, песочница-подмена.

Песочница — настоящий клиент `HttpCodeSandbox` поверх `httpx.MockTransport`: тесты идут
через протокол §7.5.12 (разбор ответа, очередь проекта, отказы), но без сети.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

from portal.code.sandbox import HttpCodeSandbox
from portal.core.settings import Settings
from portal.databases.ports import CustomerDatabase
from portal.tools.ports import SqlSandbox
from tests.conftest import close_portal, make_portal
from tests.support import Portal

DLL = b"MZ-tool-dll"


def code_settings(settings: Settings, *, build: bool = True, **code: Any) -> Settings:
    """Настройки с включённым разделом «Код»; `build` — флаг сборки, `code` — прочие поля."""
    section = settings.code.model_copy(
        update={
            "enabled": True,
            "build": settings.code.build.model_copy(update={"enabled": build, "wait_seconds": 2}),
            **code,
        }
    )
    return settings.model_copy(update={"code": section})


def succeeded(
    findings: Sequence[dict[str, Any]] = (),
    warnings: Sequence[dict[str, Any]] = (),
    bundle: Sequence[tuple[str, bytes]] = (),
    on_server: Sequence[str] = (),
) -> dict[str, Any]:
    """Ответ песочницы: собрано, с находками правил и предупреждениями по выбору."""
    return {
        "status": "succeeded",
        "sdk_version": "stub",
        "diagnostics": list(warnings),
        "diagnostics_truncated": False,
        "rules": {
            "factory": "AddressTool.GpToolFactory",
            "tools": [
                {"name": "AddressTool.AddContactTool", "title": "Добавить", "category": None}
            ],
            "findings": list(findings),
        },
        "dll": base64.b64encode(DLL).decode(),
        "bundle": [
            {"file_name": name, "content": base64.b64encode(content).decode()}
            for name, content in bundle
        ],
        "on_server": list(on_server),
    }


def failed(message: str = 'Имя "x" не существует в текущем контексте.') -> dict[str, Any]:
    """Ответ песочницы: ошибка компиляции `CS0103`."""
    return {
        "status": "failed",
        "sdk_version": "stub",
        "diagnostics": [
            {
                "severity": "error",
                "code": "CS0103",
                "file": "AddContactTool.cs",
                "line": 3,
                "column": 5,
                "end_line": 3,
                "end_column": 6,
                "message": message,
            }
        ],
        "diagnostics_truncated": False,
        "rules": None,
        "dll": None,
        "bundle": [],
        "on_server": [],
    }


def finding(rule: str = "factory") -> dict[str, Any]:
    """Находка правил GPTool."""
    return {"rule": rule, "file": None, "line": None, "column": None, "message": "Нет фабрики."}


class FakeSandboxServer:
    """Песочница за `MockTransport`: отвечает по очереди и запоминает тела запросов.

    Шаг очереди — тело ответа `200`, пара `(статус, тело)` или `asyncio.Event`
    (ответ ждёт, пока тест его не выставит); пустая очередь — `succeeded()`.
    """

    def __init__(self) -> None:
        self.replies: list[dict[str, Any] | tuple[int, dict[str, Any]] | asyncio.Event] = []
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Ответ на `POST /compile`."""
        assert request.url.path == "/compile"
        self.requests.append(json.loads(request.content))
        reply = self.replies.pop(0) if self.replies else succeeded()
        while isinstance(reply, asyncio.Event):
            await reply.wait()
            reply = self.replies.pop(0) if self.replies else succeeded()
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=body)


@asynccontextmanager
async def code_portal(
    settings: Settings,
    *,
    build: bool = True,
    llm_url: str | None = None,
    sql_sandbox: SqlSandbox | None = None,
    customer_database: CustomerDatabase | None = None,
    **code: Any,
) -> AsyncIterator[tuple[Portal, FakeSandboxServer]]:
    """Приложение с включённым разделом «Код» и песочницей-подменой.

    `llm_url` — адрес заглушки стенда: модель — настоящий клиент Bifrost, а не сценарий;
    `sql_sandbox` и `customer_database` — песочница SQL и база заказчика (§7.5.18).
    """
    target = code_settings(settings, build=build, **code)
    if llm_url is not None:
        target = target.model_copy(
            update={"llm": target.llm.model_copy(update={"base_url": f"{llm_url}/v1"})}
        )
    server = FakeSandboxServer()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    sandbox = HttpCodeSandbox(client, target.code.build)
    portal = await make_portal(
        target,
        code_sandbox=sandbox,
        scripted_model=llm_url is None,
        sql_sandbox=sql_sandbox,
        customer_database=customer_database,
    )
    try:
        yield portal, server
    finally:
        await close_portal(portal)
        await client.aclose()


async def new_project(client: httpx.AsyncClient, name: str = "AddressTool") -> dict[str, Any]:
    """Создать проект-пример и вернуть `CodeProject`."""
    response = await client.post("/api/code/projects", json={"source": "example", "name": name})
    assert response.status_code == 201, response.text
    project: dict[str, Any] = response.json()["project"]
    return project


def file_of(project: dict[str, Any], path: str) -> dict[str, Any]:
    """Файл проекта по пути."""
    found: dict[str, Any] = next(item for item in project["files"] if item["path"] == path)
    return found


def edit_answer(*files: tuple[str, str], text: str = "Готово.") -> str:
    """Ответ модели с правкой «файлы целиком» (§7.5.10)."""
    blocks = "".join(f"\n```csharp path={path}\n{content}\n```\n" for path, content in files)
    return f"{text}\n{blocks}"
