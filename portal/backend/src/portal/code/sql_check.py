"""«Проверить запрос» проекта «Код» (docs/portal-api.md §7.5.18, поток §6.6).

Параметры сервер находит в тексте сам — вне строк, комментариев, имён в кавычках и
`$…$` — и подставляет значения строковыми константами (А-140). Получившийся текст
недоверенный: он идёт тем же ходом проверки, что блок ответа модели (§7.4). Итог нигде не
сохраняется.
"""

import asyncio
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from portal.code import errors
from portal.code.service import CodeService
from portal.core.errors import AppError, feature_disabled, field_error, validation_error
from portal.core.events import EventChannel
from portal.core.logging import code_locations
from portal.core.ports import SessionAuthenticator
from portal.databases import errors as db_errors
from portal.databases.domain import selection_of
from portal.databases.service import DbService
from portal.dialogs.generation import ERROR_MESSAGES
from portal.dialogs.watch import StreamWatch
from portal.tools.dialog_tools import SqlTool
from portal.tools.sql_run import DbCheck, SqlTestRunner
from portal.tools.sql_schemas import SqlSchemaService

logger = logging.getLogger(__name__)

VALUE_MAX_CHARS = 1000
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"[1-9][0-9]*")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_COMMENT_EDGE = re.compile(r"/\*|\*/")


@dataclass(frozen=True)
class Occurrence:
    """Вхождение параметра в текст: `[start, end)` и имя с `@` или `$`."""

    start: int
    end: int
    name: str


@dataclass(frozen=True)
class CheckSource:
    """Схема для модели и проверка на образце — после проверок 1–2 (§7.5.18)."""

    schema: str
    db: DbCheck | None


def _skip_quoted(sql: str, position: int, quote: str, backslash: bool) -> int:
    """Конец участка в кавычках, открытого в `position`; незакрытый длится до конца."""
    position += 1
    while position < len(sql):
        char = sql[position]
        if backslash and char == "\\":
            position += 2
        elif char == quote:
            if not sql.startswith(quote, position + 1):
                return position + 1
            position += 2
        else:
            position += 1
    return len(sql)


def _skip_block_comment(sql: str, position: int) -> int:
    """Конец комментария `/* … */`, в том числе вложенного; незакрытый — до конца."""
    depth = 0
    for edge in _COMMENT_EDGE.finditer(sql, position):
        depth += 1 if edge[0] == "/*" else -1
        if depth == 0:
            return edge.end()
    return len(sql)


def _after(sql: str, position: int, forbidden: str) -> bool:
    """Символ перед `position` не буква, не цифра и не из `forbidden`."""
    if position == 0:
        return True
    previous = sql[position - 1]
    return not (previous.isalnum() or previous in forbidden)


def find_parameters(sql: str) -> list[Occurrence]:
    """Вхождения `@имя` и `$n` вне непрозрачных участков §7.1 (правило §7.5.18, п. 4)."""
    found: list[Occurrence] = []
    position = 0
    length = len(sql)
    while position < length:
        char = sql[position]
        if char == "'":
            position = _skip_quoted(sql, position, "'", backslash=False)
        elif char == '"':
            position = _skip_quoted(sql, position, '"', backslash=False)
        elif sql.startswith("--", position):
            end = sql.find("\n", position)
            position = length if end < 0 else end
        elif sql.startswith("/*", position):
            position = _skip_block_comment(sql, position)
        elif char == "$" and (tag := _DOLLAR_TAG.match(sql, position)) is not None:
            end = sql.find(tag[0], tag.end())
            position = length if end < 0 else end + len(tag[0])
        elif (
            char == "$" and _after(sql, position, "_$") and (n := _NUMBER.match(sql, position + 1))
        ):
            found.append(Occurrence(position, n.end(), sql[position : n.end()]))
            position = n.end()
        elif (
            char == "@"
            and _after(sql, position, "_@<")
            and (name := _NAME.match(sql, position + 1))
        ):
            found.append(Occurrence(position, name.end(), sql[position : name.end()]))
            position = name.end()
        elif (word := _WORD.match(sql, position)) is not None:
            position = word.end()
            if word[0] in ("E", "e") and sql.startswith("'", position):
                # Строка с приставкой E: обратная черта экранирует следующий символ.
                position = _skip_quoted(sql, position, "'", backslash=True)
        else:
            position += 1
    return found


def bind_parameters(sql: str, parameters: Sequence[tuple[str, str]]) -> str:
    """Сверить набор `parameters` с найденным и подставить значения (§7.5.18, А-140).

    Нет значения — `required` с `details.names`; лишнее имя — `unknown_value`; повтор —
    `invalid_format`; значение длиннее 1000 символов — `too_long`.
    """
    occurrences = find_parameters(sql)
    needed = list(dict.fromkeys(item.name for item in occurrences))
    names = [name for name, _ in parameters]
    given, known = set(names), set(needed)
    missing = [name for name in needed if name not in given]
    if missing:
        raise AppError(
            422,
            "validation_error",
            "Проверьте заполнение полей.",
            fields=[field_error("parameters", "required")],
            details={"names": missing},
        )
    if not given <= known:
        raise validation_error([field_error("parameters", "unknown_value")])
    if len(given) != len(names):
        raise validation_error([field_error("parameters", "invalid_format")])
    for index, (_, value) in enumerate(parameters):
        if len(value) > VALUE_MAX_CHARS:
            raise validation_error([field_error(f"parameters.{index}.value", "too_long")])
    values = dict(parameters)
    parts: list[str] = []
    last = 0
    for item in occurrences:
        parts.append(sql[last : item.start])
        parts.append("'" + values[item.name].replace("'", "''") + "'")
        last = item.end
    parts.append(sql[last:])
    return "".join(parts)


async def _always() -> bool:
    return True


class SqlCheckService:
    """«Проверить запрос»: проверки до потока и сам ход в своей задаче."""

    def __init__(
        self,
        projects: CodeService,
        schemas: SqlSchemaService,
        sql_tool: SqlTool,
        runner: SqlTestRunner | None,
        databases: DbService,
        sessions: SessionAuthenticator,
        *,
        dialect: str,
        sql_max_chars: int,
        recheck_seconds: float,
    ) -> None:
        """Получить зависимости явно; `runner` — `None` при выключенной песочнице SQL."""
        self._projects = projects
        self._schemas = schemas
        self._sql_tool = sql_tool
        self._runner = runner
        self._databases = databases
        self._sessions = sessions
        self._dialect = dialect
        self._sql_max_chars = sql_max_chars
        self._recheck_seconds = recheck_seconds
        self._running: set[UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def source(self, owner_id: UUID, project_id: UUID) -> CheckSource:
        """Проверки 1–2: песочница включена, у своего проекта схема или выбор подключения."""
        if self._runner is None:
            raise feature_disabled()
        project = await self._projects.project(owner_id, project_id)
        if project.schema_id is not None:
            found = await self._schemas.find(owner_id, project.schema_id)
            if found is not None:
                return CheckSource(found.content, None)
        selection = selection_of(
            project.db_connection_id, project.db_connection_title, project.db_tables
        )
        if selection is None or not self._databases.enabled:
            raise db_errors.schema_required()
        if not await self._databases.available(owner_id, selection):
            raise db_errors.connection_unavailable()
        schema = await self._databases.schema_text(owner_id, selection) or ""
        return CheckSource(schema, await self._sql_tool.db_check(owner_id, selection))

    def start(
        self,
        owner_id: UUID,
        session_id: UUID,
        project_id: UUID,
        source: CheckSource,
        sql: str,
        parameters: Sequence[tuple[str, str]],
    ) -> EventChannel:
        """Проверки 3–5 и запуск проверки; ответ — поток §6.6."""
        if not sql.strip():
            raise validation_error([field_error("sql", "required")])
        if len(sql) > self._sql_max_chars:
            raise validation_error([field_error("sql", "too_long")])
        text = bind_parameters(sql, parameters)
        if project_id in self._running:
            raise errors.sql_check_in_progress()
        assert self._runner is not None  # проверено в `source`
        self._running.add(project_id)
        channel = EventChannel()
        task = asyncio.create_task(
            self._run(self._runner, owner_id, session_id, project_id, source, text, channel)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return channel

    async def _run(
        self,
        runner: SqlTestRunner,
        owner_id: UUID,
        session_id: UUID,
        project_id: UUID,
        source: CheckSource,
        text: str,
        channel: EventChannel,
    ) -> None:
        """Ход проверки под наблюдением; отметка проекта снимается при любом исходе."""
        try:
            watch = StreamWatch(
                channel, self._sessions, session_id, _always, math.inf, self._recheck_seconds
            )
            try:
                _, reason = await watch.result(
                    runner.check_text(
                        owner_id,
                        source.schema,
                        self._dialect,
                        text,
                        lambda state: channel.emit("sql_run", state.event),
                        db=source.db,
                    )
                )
            except Exception as error:
                logger.error(
                    "sql check failed",
                    extra={"error_type": type(error).__name__, "trace": code_locations(error)},
                )
                channel.emit(
                    "error",
                    {"code": "internal_error", "message": ERROR_MESSAGES["internal_error"]},
                )
                return
            finally:
                watch.close()
            if reason is None:
                channel.emit("done", {})
            elif reason != "stopped":
                channel.emit("error", {"code": reason, "message": ERROR_MESSAGES[reason]})
        finally:
            self._running.discard(project_id)
            channel.finish()

    async def shutdown(self) -> None:
        """Отменить идущие проверки при остановке процесса."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
