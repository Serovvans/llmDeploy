"""Проверка запросов ответа на тестовых данных — сценарий (docs/portal-api.md §7.4).

Блоки `sql` ответа проверяются по одному: место песочницы, тестовые данные от модели,
исполнение, до `sql.sandbox.fix_attempts` исправлений по ошибке исполнения. Разбор
операторов здесь — вторая линия защиты: первая — права роли песочницы и способ передачи
текста серверу, их держит порт `SandboxSlot`. Тексты запросов, схем, данных и сообщений
СУБД в журнал не пишутся.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from portal.core.settings import SqlSandboxSettings
from portal.dialogs.context import data_block
from portal.llm.ports import (
    ChatModel,
    ChatRequest,
    ContextOverflowError,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    TextPart,
)
from portal.tools.ports import (
    SandboxQueryError,
    SandboxRun,
    SandboxSetupError,
    SandboxSlot,
    SandboxTable,
    SandboxUnavailableError,
    SqlSandbox,
)
from portal.tools.sql_check import answer_sql_blocks

logger = logging.getLogger(__name__)

StatementKind = Literal[
    "select", "insert", "insert_values", "update", "delete", "merge", "create_table", "other"
]
RunStatus = Literal[
    "pending",
    "running",
    "passed",
    "fixed",
    "failed",
    "not_checked",
    "unavailable",
    "timeout",
    "stopped",
]
SkipReason = Literal[
    "unparsed",
    "multiple_statements",
    "statement_not_supported",
    "block_limit",
    "test_data_failed",
]

# Первая строка сообщения пользователя — ровно эта фраза: по ней запрос узнаёт заглушка (§12.1).
DATA_PROMPT = "Подготовь тестовые данные."
FIX_PROMPT = "Исправь запрос."
_DATA_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"statements": {"type": "array", "items": {"type": "string"}}},
    "required": ["statements"],
    "additionalProperties": False,
}
_CHECKED: frozenset[StatementKind] = frozenset(
    {"select", "insert", "insert_values", "update", "delete", "merge"}
)
_SETUP: frozenset[StatementKind] = frozenset({"create_table", "insert_values"})
_MODEL_ERRORS = (ModelUnavailableError, ModelOverloadedError, ContextOverflowError)
_UNFINISHED: frozenset[RunStatus] = frozenset({"pending", "running"})


class StatementClassifier(Protocol):
    """Разбор текста на операторы до исполнения; реализация — на `sqlglot`."""

    async def statements(self, sql: str, dialect: str) -> Sequence[StatementKind] | None:
        """Виды операторов по порядку; `None` — текст не разобран либо превышен предел."""
        ...


def _table(table: SandboxTable, *, named: bool) -> dict[str, Any]:
    data: dict[str, Any] = {"table": table.table} if named else {}
    data["columns"] = list(table.columns)
    data["rows"] = [list(row) for row in table.rows]
    data["truncated"] = table.truncated
    return data


@dataclass
class _Block:
    """Запись `SqlRun` об одном блоке `sql` ответа."""

    index: int
    line: int
    text: str
    kind: StatementKind = "other"
    status: RunStatus = "pending"
    reason: SkipReason | None = None
    fixture: Sequence[SandboxTable] | None = None
    result: SandboxTable | None = None
    affected_rows: int | None = None
    error: str | None = None
    fixed_sql: str | None = None

    def skip(self, reason: SkipReason) -> None:
        """Блок не проверялся."""
        self.status, self.reason = "not_checked", reason

    def data(self) -> dict[str, Any]:
        """Запись в виде объекта контракта."""
        fixture = self.fixture
        return {
            "index": self.index,
            "line": self.line,
            "status": self.status,
            "reason": self.reason,
            "fixture": None if fixture is None else [_table(t, named=True) for t in fixture],
            "result": None if self.result is None else _table(self.result, named=False),
            "affected_rows": self.affected_rows,
            "error": self.error,
            "fixed_sql": self.fixed_sql,
        }


class SqlTestRunner:
    """Проверяет блоки `sql` готового ответа в песочнице и сообщает состояние целиком."""

    def __init__(
        self,
        sandbox: SqlSandbox,
        model: ChatModel,
        classifier: StatementClassifier,
        settings: SqlSandboxSettings,
    ) -> None:
        """Получить зависимости явно."""
        self._sandbox = sandbox
        self._model = model
        self._classifier = classifier
        self._settings = settings

    async def run(
        self,
        user_id: UUID,
        schema: str,
        dialect: str,
        content: str,
        report: Callable[[Mapping[str, Any]], None],
    ) -> None:
        """Проверить блоки ответа по порядку; `report` получает `SqlRun` при каждой смене.

        В последнем сообщении ни один блок не ждёт и не идёт. Отмена задачи (остановка,
        потеря сессии) обрывает проверку: непроверенные блоки получают `stopped`, это
        состояние сообщается, и отмена идёт дальше.
        """
        blocks = [
            _Block(block.index, block.line, block.text)
            for block in answer_sql_blocks(content)
            if block.closed
        ]

        def publish() -> None:
            report({"blocks": [block.data() for block in blocks]})

        try:
            for position, block in enumerate(blocks):
                await self._select(position, block, dialect)
            publish()
            for block in blocks:
                if block.status == "pending":
                    await self._check(user_id, schema, dialect, block, publish)
        except asyncio.CancelledError:
            for block in blocks:
                if block.status in _UNFINISHED:
                    block.status = "stopped"
            publish()
            raise

    async def _select(self, position: int, block: _Block, dialect: str) -> None:
        """Отобрать блок: исполняется только один оператор разрешённого вида."""
        if position >= self._settings.max_blocks:
            block.skip("block_limit")
            return
        kinds = await self._classifier.statements(block.text, dialect)
        if not kinds:
            block.skip("unparsed")
        elif len(kinds) > 1:
            block.skip("multiple_statements")
        elif kinds[0] not in _CHECKED:
            block.skip("statement_not_supported")
        else:
            block.kind = kinds[0]

    async def _sole(
        self, sql: str, dialect: str, allowed: frozenset[StatementKind]
    ) -> StatementKind | None:
        """Вид оператора, если текст — ровно один оператор из `allowed`; иначе `None`."""
        kinds = await self._classifier.statements(sql, dialect)
        if kinds is None or len(kinds) != 1 or kinds[0] not in allowed:
            return None
        return kinds[0]

    async def _check(
        self, user_id: UUID, schema: str, dialect: str, block: _Block, publish: Callable[[], None]
    ) -> None:
        """Проверить один блок: место, общий срок с момента его получения, исход."""
        try:
            async with self._sandbox.slot(user_id) as slot:
                block.status = "running"
                publish()
                try:
                    async with asyncio.timeout(self._settings.total_timeout_seconds):
                        await self._attempts(slot, schema, dialect, block)
                except TimeoutError:
                    block.status = "timeout"
        except SandboxUnavailableError:
            block.status = "unavailable"
        except Exception as error:
            # Сбой, которого порт не обещал: исход получает и этот блок, а итоги уже
            # проверенных блоков не теряются. В журнал — только тип ошибки.
            logger.error("sql test run block failed", extra={"error": type(error).__name__})
            block.status = "unavailable"
        publish()

    async def _attempts(self, slot: SandboxSlot, schema: str, dialect: str, block: _Block) -> None:
        """Тестовые данные, запрос и исправления; исход записывается в блок."""
        setup = await self._test_data(schema, block.text, dialect)
        if setup is None:
            block.skip("test_data_failed")
            return
        sql, kind = block.text, block.kind
        first_error = last_error = None
        for attempt in range(self._settings.fix_attempts + 1):
            if attempt:
                fixed = await self._fixed(schema, setup, sql, last_error or "", dialect)
                if fixed is None:
                    continue  # негодное исправление — неудачная попытка
                sql, kind = fixed
            try:
                run = await slot.run(setup, sql)
            except SandboxSetupError:
                block.skip("test_data_failed")
                return
            except SandboxQueryError as failure:
                last_error = failure.message
                first_error = first_error or last_error
                continue
            self._succeed(block, run, kind, first_error, sql)
            return
        block.status, block.error = "failed", last_error

    @staticmethod
    def _succeed(
        block: _Block, run: SandboxRun, kind: StatementKind, first_error: str | None, sql: str
    ) -> None:
        block.fixture, block.result = run.fixture, run.result
        # У запроса на чтение числа затронутых строк нет (§7.4).
        block.affected_rows = None if kind == "select" else run.affected_rows
        if first_error is None:
            block.status = "passed"
        else:
            # Запрос исправлен: показывается ошибка исходного и текст выполнившегося.
            block.status, block.error, block.fixed_sql = "fixed", first_error, sql

    async def _complete(
        self, system: str, user: str, json_schema: Mapping[str, Any] | None = None
    ) -> str | None:
        """Ответ модели; `None` — модель не ответила."""
        request = ChatRequest(
            [ModelMessage("system", [TextPart(system)]), ModelMessage("user", [TextPart(user)])],
            "low",
            self._settings.max_output_tokens,
            json_schema,
        )
        try:
            return await self._model.complete(request)
        except _MODEL_ERRORS:
            return None

    async def _test_data(self, schema: str, sql: str, dialect: str) -> list[str] | None:
        """Операторы подготовки от модели; `None` — пригодных данных нет.

        Каждый оператор — ровно один `CREATE TABLE` без `AS SELECT` либо `INSERT … VALUES`.
        """
        user = "\n".join([DATA_PROMPT, data_block("схема", schema), data_block("запрос", sql)])
        reply = await self._complete(self._settings.data_system_prompt, user, _DATA_SCHEMA)
        try:
            statements = json.loads(reply or "")["statements"]
        except (ValueError, TypeError, KeyError):
            return None
        if not isinstance(statements, list) or not statements:
            return None
        for statement in statements:
            if not isinstance(statement, str):
                return None
            if await self._sole(statement, dialect, _SETUP) is None:
                return None
        return statements

    async def _fixed(
        self, schema: str, setup: Sequence[str], sql: str, error: str, dialect: str
    ) -> tuple[str, StatementKind] | None:
        """Исправленный запрос от модели и его вид; `None` — попытка неудачна.

        Сообщение СУБД — данные в размеченном блоке, а не указания. Исправление проходит
        тот же отбор, что блок ответа: один оператор разрешённого вида.
        """
        user = "\n".join(
            [
                FIX_PROMPT,
                data_block("схема", schema),
                data_block("подготовка", ";\n".join(setup)),
                data_block("запрос", sql),
                data_block("ошибка", error),
            ]
        )
        reply = await self._complete(self._settings.fix_system_prompt, user)
        fixed = next((b.text.strip() for b in answer_sql_blocks(reply or "") if b.closed), None)
        if fixed is None:
            return None
        kind = await self._sole(fixed, dialect, _CHECKED)
        return None if kind is None else (fixed, kind)
