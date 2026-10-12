"""Порт песочницы проверки SQL на тестовых данных (docs/portal-api.md §7.4, §13.3).

Общий срок проверки блока, запросы к модели, разбор операторов до исполнения и снятие
имён схем — в сценарии (`portal/tools/sql_run.py`): порт ничего не знает о модели, а
тексты операторов считает враждебными и сам их не разбирает.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class SandboxTable:
    """Таблица значений из песочницы: ячейки — текст СУБД либо `None` (SQL `NULL`)."""

    table: str
    columns: Sequence[str]
    rows: Sequence[Sequence[str | None]]
    truncated: bool


@dataclass(frozen=True)
class SandboxRun:
    """Итог одной выполнившейся попытки.

    `result` — что вернул запрос (`table` — пустая строка); `None` — запрос строк не
    возвращает. `affected_rows` — число строк из сообщения сервера о завершении команды;
    `None` — сервер его не сообщил или чтение результата прервано на пределе строк. Порт
    не различает чтение и изменение: показывать ли число, решает сценарий по виду оператора.
    """

    fixture: Sequence[SandboxTable]
    result: SandboxTable | None
    affected_rows: int | None


class SandboxUnavailableError(Exception):
    """Проверить негде: нет соединения, место не выдано либо оператор завершил транзакцию."""


class SandboxSetupError(Exception):
    """СУБД отвергла подготовку либо тестовые данные больше предела."""


class SandboxQueryError(Exception):
    """Ошибка исполнения запроса либо результат больше предела; `message` уже обрезано.

    `fixture` — тестовые данные, прочитанные перед запросом: они есть при любой причине.
    """

    def __init__(self, message: str, fixture: Sequence[SandboxTable]) -> None:
        """Запомнить сообщение СУБД (недоверенный текст) и тестовые данные."""
        super().__init__()
        self.message = message
        self.fixture = fixture


class SandboxSlot(Protocol):
    """Место песочницы со своей ролью входа."""

    async def run(self, setup: Sequence[str], query: str) -> SandboxRun:
        """Одна попытка: новое соединение и транзакция, которая всегда откатывается.

        Имена схем в текстах уже сняты сценарием. Отмена задачи (общий срок, остановка,
        потеря сессии) принудительно завершает сеанс на сервере. Исключения:
        `SandboxUnavailableError`, `SandboxSetupError`, `SandboxQueryError`.
        """
        ...


class SqlSandbox(Protocol):
    """Песочница: не больше `sql.sandbox.max_parallel` мест и одно на сотрудника."""

    def slot(self, user_id: UUID) -> AbstractAsyncContextManager[SandboxSlot]:
        """Дождаться чистого места и держать его, пока проверяется блок.

        Не дождался за `sql.sandbox.total_timeout_seconds`, не убедился в чистоте либо
        рабочих мест нет — `SandboxUnavailableError`.
        """
        ...
