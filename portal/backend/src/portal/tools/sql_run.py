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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from portal.core.settings import SqlSandboxSettings
from portal.databases.domain import (
    Connection,
    InventedReason,
    StoredSnapshot,
    copy_plan,
    quote_ident,
    resolve_table,
    sample_origin,
)
from portal.databases.ports import (
    DbRightsExcessError,
    DbUnavailableError,
    SampleRequest,
    SampleRows,
)
from portal.dialogs.context import data_block
from portal.dialogs.ports import RunReport
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
    SampleTable,
    SandboxQueryError,
    SandboxRun,
    SandboxSetupError,
    SandboxSlot,
    SandboxTable,
    SandboxUnavailableError,
    SqlSandbox,
)
from portal.tools.sql_check import answer_sql_blocks
from portal.tools.sqlstates import sqlstate_text

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
    "table_name_conflict",
    "block_limit",
    "test_data_failed",
]
# Причина `unavailable` у блока с подключением (1.0); `None` — песочница недоступна.
UnavailableReason = Literal[
    "connection_unavailable", "connection_unusable", "db_busy", "db_unavailable"
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


@dataclass(frozen=True)
class ParsedSql:
    """Разбор текста до исполнения.

    `bare` — текст единственного оператора без имён схем у таблиц: таким он уходит в
    песочницу, у проверки схема одна. `name_conflict` — одно имя таблицы стоит с разными
    схемами либо и со схемой, и без неё: без схем это был бы уже другой запрос.
    `tables` — источники и цели единственного оператора `(схема или None, имя)` без имён
    общих табличных выражений; имена — по правилам PostgreSQL (1.0, проверка на образце).
    """

    kinds: Sequence[StatementKind]
    bare: str
    name_conflict: bool
    tables: Sequence[tuple[str | None, str]] = ()


class StatementClassifier(Protocol):
    """Разбор текста на операторы до исполнения; реализация — на `sqlglot`."""

    async def statements(self, sql: str, dialect: str) -> ParsedSql | None:
        """Разбор текста; `None` — текст не разобран либо превышен предел."""
        ...


@dataclass(frozen=True)
class SampleSource:
    """Подключение выбора с текущим снимком и чтением образца одним обращением (§7.6.5)."""

    connection: Connection
    snapshot: StoredSnapshot
    read: Callable[[Sequence[SampleRequest]], Awaitable[Sequence[SampleRows]]]


@dataclass(frozen=True)
class DbCheck:
    """Проверка с подключением (§7.4, «Проверка на образце»).

    `title` — название подключения на момент вопроса; `sample_rows` — `db.sample_rows_check`;
    `source` — `None`, когда выбор недоступен: блоки получают `unavailable`.
    """

    title: str
    sample_rows: int
    source: SampleSource | None


@dataclass(frozen=True)
class _Failure:
    """Ошибка проверяемого запроса: основное сообщение, код SQLSTATE и шаг."""

    message: str
    sqlstate: str | None
    at_parse: bool

    def shown(self, sampled: bool) -> tuple[str, Literal["message", "sqlstate"]]:
        """Что идёт модели и в сохранённую запись (§7.4, «Сообщение СУБД при образце»).

        При таблицах образца ошибка исполнения — только код SQLSTATE с именем условия:
        значение строки базы может оказаться в тексте сообщения.
        """
        if sampled and not self.at_parse and self.sqlstate is not None:
            return sqlstate_text(self.sqlstate), "sqlstate"
        return self.message, "message"


def sample_ddl(table: SampleTable) -> str:
    """Таблица образца, как её создаёт песочница, — без строк (для модели, §12.1)."""
    parts = [f"{quote_ident(name)} {kind}" for name, kind in table.columns]
    if table.primary_key:
        parts.append(f"PRIMARY KEY ({', '.join(quote_ident(k) for k in table.primary_key)})")
    return f"CREATE TABLE {quote_ident(table.name)} ({', '.join(parts)})"


@dataclass
class _Block:
    """Запись `SqlRun` об одном блоке `sql` ответа."""

    index: int
    line: int
    text: str
    bare: str = ""
    kind: StatementKind = "other"
    tables: Sequence[tuple[str | None, str]] = ()
    status: RunStatus = "pending"
    reason: SkipReason | UnavailableReason | None = None
    fixture: Sequence[SandboxTable] | None = None
    result: SandboxTable | None = None
    affected_rows: int | None = None
    failure: _Failure | None = None
    fixed_sql: str | None = None
    # С подключением (1.0): `db` записи, таблицы образца в песочнице, причины придуманных.
    db: dict[str, Any] | None = None
    samples: frozenset[str] = frozenset()
    cut_samples: frozenset[str] = frozenset()
    reasons: dict[str, InventedReason] = field(default_factory=dict)

    def skip(self, reason: SkipReason) -> None:
        """Блок не проверялся."""
        self.status, self.reason = "not_checked", reason

    def unavailable(self, reason: UnavailableReason) -> None:
        """Проверить негде: подключение или база."""
        self.status, self.reason = "unavailable", reason

    def _fixture_table(self, table: SandboxTable, strip: bool) -> dict[str, Any]:
        origin, why = "invented", None
        if table.table in self.samples:
            origin = "sample"
        elif self.db is not None:
            why = self.reasons.get(table.table) or self.reasons.get(
                table.table.lower(), "not_in_snapshot"
            )
        return {
            "table": table.table,
            "columns": list(table.columns),
            "rows": None if strip else [list(row) for row in table.rows],
            "truncated": table.truncated or table.table in self.cut_samples,
            "origin": origin,
            "invented_reason": why,
        }

    def data(self, *, stored: bool) -> dict[str, Any]:
        """Запись в виде объекта контракта: для события или для хранения.

        В сохранённой записи блока с подключением строк нет (§7.4, правило 22).
        """
        strip = stored and self.db is not None
        error: str | None = None
        error_kind: str | None = None
        if self.failure is not None:
            if stored:
                error, error_kind = self.failure.shown(bool(self.samples))
            else:
                error, error_kind = self.failure.message, "message"
        result = None
        if self.result is not None:
            result = {
                "columns": list(self.result.columns),
                "rows": None if strip else [list(row) for row in self.result.rows],
                "row_count": len(self.result.rows),
                "truncated": self.result.truncated,
            }
        fixture = self.fixture
        return {
            "index": self.index,
            "line": self.line,
            "status": self.status,
            "reason": self.reason,
            "db": self.db,
            "fixture": None
            if fixture is None
            else [self._fixture_table(t, strip) for t in fixture],
            "result": result,
            "affected_rows": self.affected_rows,
            "error": error,
            "error_kind": error_kind,
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
        report: Callable[[RunReport], None],
        *,
        db: DbCheck | None = None,
        only: int | None = None,
    ) -> None:
        """Проверить блоки ответа по порядку; `report` получает `SqlRun` при каждой смене.

        `only` — «Проверить снова»: только блок с этим `index` (его место среди блоков
        ответа для предела `max_blocks` прежнее). В последнем сообщении ни один блок не ждёт
        и не идёт. Отмена задачи (остановка, потеря сессии) обрывает проверку:
        непроверенные блоки получают `stopped`, это состояние сообщается, отмена идёт дальше.
        """
        found = [block for block in answer_sql_blocks(content) if block.closed]
        blocks = [
            (position, _Block(block.index, block.line, block.text))
            for position, block in enumerate(found)
            if only is None or block.index == only
        ]
        await self._run(user_id, schema, dialect, blocks, report, db)

    async def check_text(
        self,
        user_id: UUID,
        schema: str,
        dialect: str,
        sql: str,
        report: Callable[[RunReport], None],
        *,
        db: DbCheck | None = None,
    ) -> None:
        """«Проверить запрос» проекта (§7.5.18): один блок, `index: 0`, `line: 1`."""
        await self._run(user_id, schema, dialect, [(0, _Block(0, 1, sql))], report, db)

    async def _run(
        self,
        user_id: UUID,
        schema: str,
        dialect: str,
        positioned: Sequence[tuple[int, _Block]],
        report: Callable[[RunReport], None],
        db: DbCheck | None,
    ) -> None:
        blocks = [block for _, block in positioned]

        def publish() -> None:
            report(
                RunReport(
                    {"blocks": [block.data(stored=False) for block in blocks]},
                    {"blocks": [block.data(stored=True) for block in blocks]},
                )
            )

        try:
            for position, block in positioned:
                await self._select(position, block, dialect)
            publish()
            for block in blocks:
                if block.status == "pending":
                    await self._check(user_id, schema, dialect, block, publish, db)
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
        parsed = await self._classifier.statements(block.text, dialect)
        if parsed is None or not parsed.kinds:
            block.skip("unparsed")
        elif len(parsed.kinds) > 1:
            block.skip("multiple_statements")
        elif parsed.kinds[0] not in _CHECKED:
            block.skip("statement_not_supported")
        elif parsed.name_conflict:
            block.skip("table_name_conflict")
        else:
            block.kind, block.bare, block.tables = parsed.kinds[0], parsed.bare, parsed.tables

    async def _sole(
        self, sql: str, dialect: str, allowed: frozenset[StatementKind]
    ) -> ParsedSql | None:
        """Разбор текста, если он — ровно один оператор из `allowed`; иначе `None`."""
        parsed = await self._classifier.statements(sql, dialect)
        if parsed is None or len(parsed.kinds) != 1 or parsed.kinds[0] not in allowed:
            return None
        return parsed

    async def _check(
        self,
        user_id: UUID,
        schema: str,
        dialect: str,
        block: _Block,
        publish: Callable[[], None],
        db: DbCheck | None,
    ) -> None:
        """Проверить один блок: место, общий срок с момента его получения, исход."""
        try:
            async with self._sandbox.slot(user_id) as slot:
                block.status = "running"
                publish()
                try:
                    async with asyncio.timeout(self._settings.total_timeout_seconds):
                        await self._attempts(slot, schema, dialect, block, db)
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

    async def _samples(self, block: _Block, db: DbCheck) -> list[SampleTable] | None:
        """Таблицы блока по снимку и образец одним обращением к базе (шаги 1–2).

        Образец читается после получения места (А-144); неудача — `unavailable` с
        причиной, без повтора. `None` — блок уже получил исход.
        """
        block.db = {"connection_title": db.title, "sample_rows": db.sample_rows}
        excluded: list[dict[str, str]] = []
        block.db["excluded_columns"] = excluded
        source = db.source
        if source is None:
            block.unavailable("connection_unavailable")
            return None
        chosen: list[tuple[Mapping[str, Any], Any]] = []
        for schema_name, name in block.tables:
            table = resolve_table(source.connection, source.snapshot, schema_name, name)
            why: InventedReason | None = "not_in_snapshot"
            if table is not None:
                why = sample_origin(source.connection, table)
            if why is not None or table is None:
                block.reasons[name] = why or "not_in_snapshot"
                continue
            if all(table is not other for other, _ in chosen):
                chosen.append((table, copy_plan(source.connection, table)))
        if not chosen:
            return []
        requests = [SampleRequest(t["schema"], t["name"], plan.columns) for t, plan in chosen]
        try:
            read = await source.read(requests)
        except DbRightsExcessError:
            block.unavailable("connection_unusable")
            return None
        except DbUnavailableError as error:
            block.unavailable("db_busy" if error.reason == "busy" else "db_unavailable")
            return None
        samples = []
        cut = set()
        for (sampled, plan), rows in zip(chosen, read, strict=True):
            samples.append(_sample_table(sampled, plan.excluded, rows))
            excluded.extend(
                {"table": sampled["name"], "column": column, "reason": reason}
                for column, reason in plan.excluded
            )
            if rows.truncated:
                cut.add(sampled["name"])
        block.samples = frozenset(item.name for item in samples)
        block.cut_samples = frozenset(cut)
        return samples

    async def _attempts(
        self, slot: SandboxSlot, schema: str, dialect: str, block: _Block, db: DbCheck | None
    ) -> None:
        """Образец, тестовые данные, запрос и исправления; исход записывается в блок."""
        samples: list[SampleTable] = []
        if db is not None:
            found = await self._samples(block, db)
            if found is None:
                return
            samples = found
        ready = [sample_ddl(table) for table in samples]
        # С подключением модель готовит данные только для таблиц не из образца (шаг 3).
        if db is None or block.reasons:
            prepared = await self._test_data(schema, block.text, dialect, ready)
            if prepared is None:
                block.skip("test_data_failed")
                return
        else:
            prepared = ([], [])
        # Модели и пользователю — тексты как написаны; в песочницу — без имён схем.
        setup, bare_setup = prepared
        sql, bare, kind = block.text, block.bare, block.kind
        sampled = bool(samples)
        first: _Failure | None = None
        last: _Failure | None = None
        last_fixture: Sequence[SandboxTable] | None = None
        for attempt in range(self._settings.fix_attempts + 1):
            if attempt:
                error = last.shown(sampled)[0] if last is not None else ""
                fixed = await self._fixed(schema, [*ready, *setup], sql, error, dialect)
                if fixed is None:
                    continue  # негодное исправление — неудачная попытка
                sql, parsed = fixed
                bare, kind = parsed.bare, parsed.kinds[0]
            try:
                run = await slot.run(bare_setup, bare, samples)
            except SandboxSetupError:
                block.skip("test_data_failed")
                return
            except SandboxQueryError as failure:
                last = _Failure(failure.message, failure.sqlstate, failure.at_parse)
                last_fixture = failure.fixture
                first = first or last
                continue
            self._succeed(block, run, kind, first, sql)
            return
        block.status, block.failure, block.fixture = "failed", last, last_fixture

    @staticmethod
    def _succeed(
        block: _Block, run: SandboxRun, kind: StatementKind, first: _Failure | None, sql: str
    ) -> None:
        block.fixture, block.result = run.fixture, run.result
        # У запроса на чтение числа затронутых строк нет (§7.4).
        block.affected_rows = None if kind == "select" else run.affected_rows
        if first is None:
            block.status = "passed"
        else:
            # Запрос исправлен: показывается ошибка исходного и текст выполнившегося.
            block.status, block.failure, block.fixed_sql = "fixed", first, sql

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

    async def _test_data(
        self, schema: str, sql: str, dialect: str, ready: Sequence[str] = ()
    ) -> tuple[list[str], list[str]] | None:
        """Операторы подготовки от модели и они же без имён схем; `None` — данных нет.

        Каждый оператор — ровно один `CREATE TABLE` без `AS SELECT` либо `INSERT … VALUES`.
        `ready` — DDL таблиц образца без строк: модель их не создаёт (§12.1).
        """
        parts = [DATA_PROMPT, data_block("схема", schema)]
        if ready:
            parts.append(data_block("готовые_таблицы", ";\n".join(ready)))
        parts.append(data_block("запрос", sql))
        reply = await self._complete(
            self._settings.data_system_prompt, "\n".join(parts), _DATA_SCHEMA
        )
        try:
            statements = json.loads(reply or "")["statements"]
        except (ValueError, TypeError, KeyError):
            return None
        if not isinstance(statements, list) or not statements:
            return None
        bare = []
        for statement in statements:
            if not isinstance(statement, str):
                return None
            parsed = await self._sole(statement, dialect, _SETUP)
            if parsed is None:
                return None
            bare.append(parsed.bare)
        return statements, bare

    async def _fixed(
        self, schema: str, setup: Sequence[str], sql: str, error: str, dialect: str
    ) -> tuple[str, ParsedSql] | None:
        """Исправленный запрос от модели и его разбор; `None` — попытка неудачна.

        Сообщение СУБД — данные в размеченном блоке, а не указания. Исправление проходит
        тот же отбор, что блок ответа: один оператор разрешённого вида без совпадения
        имён таблиц.
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
        parsed = await self._sole(fixed, dialect, _CHECKED)
        return None if parsed is None or parsed.name_conflict else (fixed, parsed)


def _sample_table(
    table: Mapping[str, Any],
    excluded: Sequence[tuple[str, str]],
    rows: SampleRows,
) -> SampleTable:
    """Таблица образца для песочницы: все столбцы, непереданные — `NULL` (§7.4, шаг 4).

    Ключ — только если ни один его столбец не исключён и не двоичный.
    """
    columns = [(column["name"], column["sandbox_type"]) for column in table["columns"]]
    skipped = {name for name, _ in excluded}
    key = [column["name"] for column in table["columns"] if column["primary_key"]]
    if any(name in skipped for name in key):
        key = []
    position = {name: index for index, name in enumerate(rows.columns)}
    data = [
        [row[position[name]] if name in position else None for name, _ in columns]
        for row in rows.rows
    ]
    return SampleTable(table["name"], columns, key, data)
