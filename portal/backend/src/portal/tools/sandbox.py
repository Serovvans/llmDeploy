"""Песочница проверки SQL: порт `SqlSandbox` на asyncpg (docs/portal-api.md §7.4).

Всё, что уходит в песочницу и приходит из неё, — враждебное. На этом модуле, а не на
разборе в сценарии, держатся «ровно один оператор» и «транзакция всегда откатывается»:
- недоверенный текст уходит только подготовленным оператором (расширенный протокол):
  текст из нескольких команд отвергает сам сервер; `Connection.execute` без аргументов
  (простой протокол) — только для собственных операторов без чужого текста;
- в собственные операторы чужой текст не подставляется: значения — параметрами, имена —
  из системного каталога, идентификаторами в кавычках, функции — с именем схемы;
- после каждого недоверенного оператора сверяется, что транзакция та же;
- результат и тестовые данные читаются по одной строке со счётом байт;
- сеансы завершаются под управляющей ролью, под ней нет недоверенного текста.

Счёт мест, занятость сотрудников и выведенные из работы места живут в памяти процесса:
`portal-api` — один процесс (§13.5).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from portal.core.settings import SqlSandboxSettings
from portal.databases.customer import sandbox_type
from portal.tools.ports import (
    SampleTable,
    SandboxQueryError,
    SandboxRun,
    SandboxSetupError,
    SandboxSlot,
    SandboxTable,
    SandboxUnavailableError,
)

logger = logging.getLogger(__name__)

# `CREATE EXTENSION postgis` при инициализации песочницы ставит расширение в `public`.
_POSTGIS_SCHEMA = "public"
_SPATIAL_TYPES = frozenset({"geometry", "geography"})
_ELLIPSIS = "…"
_RESULT_TOO_LARGE = "result is too large"
_RESULT_UNREADABLE = "result cannot be read"
_HAS_PARAMETERS = "query has parameters"
# Типы, которые драйвер иначе читал бы типами Python — с потерей («1 mon» стал бы
# «30 days») и с отказом вне их диапазона (дата до нашей эры, год больше 9999).
_TEXT_TYPES = ("date", "time", "timetz", "timestamp", "timestamptz", "interval")
_POLL_SECONDS = 0.05
# Сбои соединения и драйвера: для проверки это «песочница недоступна».
_FAILURES = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
    OSError,
    TimeoutError,
)
# Драйвер не разобрал результат запроса: значение вне диапазона типа Python, столбец
# псевдотипа системного каталога (`anyarray`, `pg_node_tree`), составной тип с полем,
# которое драйвер читает только текстом (`geometry` в `ST_Dump`).
_UNREADABLE = (
    ValueError,
    ArithmeticError,
    asyncpg.InternalClientError,
    asyncpg.UnsupportedClientFeatureError,
)
# Драйвер не смог вернуть значение серверу параметром.
_UNSENDABLE = (asyncpg.InterfaceError, asyncpg.InternalClientError)

_GUARD = (
    "SELECT pg_catalog.set_config('search_path', $1, true),"
    " pg_catalog.set_config('statement_timeout', $2, true),"
    " pg_catalog.set_config('lock_timeout', $3, true)"
)
_XACT_ID = "SELECT pg_catalog.pg_current_xact_id()::pg_catalog.text"
_TABLE_COLUMNS = """
SELECT c.relname, a.attname, tn.nspname AS type_schema, t.typname
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
JOIN pg_catalog.pg_namespace AS tn ON tn.oid = t.typnamespace
WHERE n.nspname = $1 AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.oid, a.attnum
"""
_TYPE_NAMES = """
SELECT t.oid, n.nspname, t.typname
FROM pg_catalog.pg_type AS t
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
WHERE t.oid = ANY($1::pg_catalog.oid[])
"""
# Сколько сеансов роли нашлось; каждому из них отправлен сигнал завершения.
_TERMINATE = (
    "SELECT pg_catalog.count(pg_catalog.pg_terminate_backend(pid))"
    " FROM pg_catalog.pg_stat_activity WHERE usename = $1"
)


class _TransactionEndedError(Exception):
    """Недоверенный оператор завершил транзакцию проверки: место выводится из работы."""


class _TooLargeError(Exception):
    """Данных из песочницы больше предела: чтение прекращено."""


class _UnreadableError(Exception):
    """Драйвер не смог прочитать значение результата: чтение прекращено."""


class _ParseFailedError(Exception):
    """Сервер отверг проверяемый запрос на шаге `Parse`/`Describe`, до исполнения."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error


@dataclass(frozen=True)
class _Rejected:
    """СУБД отвергла недоверенный оператор; `message` уже обрезано (только поле `M`)."""

    message: str
    sqlstate: str | None = None
    at_parse: bool = False


@dataclass(frozen=True)
class _Rows:
    """Что вернул проверяемый запрос — ещё значениями драйвера."""

    attributes: Sequence[Any]
    records: Sequence[Any]
    truncated: bool
    affected_rows: int | None


def _ident(name: str) -> str:
    """Имя идентификатором в кавычках."""
    return '"' + name.replace('"', '""') + '"'


def _text_of(value_sql: str, type_schema: str, type_name: str) -> str:
    """Выражение «значение как текст СУБД»; `geometry` и `geography` — в WKT."""
    if type_schema == _POSTGIS_SCHEMA and type_name in _SPATIAL_TYPES:
        return f"{_ident(_POSTGIS_SCHEMA)}.st_astext({value_sql})"
    return f"({value_sql})::pg_catalog.text"


def _size(value: object) -> int:
    """Оценка размера прочитанного значения в байтах."""
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    return len(repr(value))


def _sendable(value: object) -> object:
    """Значение в виде, который драйвер принимает параметром.

    Запись внутри массива драйвер принял бы за вложенный массив — она передаётся кортежем.
    """
    if isinstance(value, asyncpg.Record):
        return tuple(_sendable(item) for item in value.values())
    if isinstance(value, list):
        return [_sendable(item) for item in value]
    return value


def _count(status: str | None) -> int | None:
    """Число строк из сообщения о завершении команды (`UPDATE 3`, `INSERT 0 2`)."""
    last = (status or "").rpartition(" ")[2]
    return int(last) if last.isdecimal() else None


class AsyncpgSqlSandbox:
    """Места песочницы: у каждого своя роль входа; завершает сеансы управляющая роль."""

    def __init__(
        self, settings: SqlSandboxSettings, run_password: str, control_password: str
    ) -> None:
        """Запомнить адрес и пароли ролей; соединений до первой проверки нет."""
        self._settings = settings
        self._run_password = run_password
        self._control_password = control_password
        self._free = set(range(1, settings.max_parallel + 1))
        self._retired: set[int] = set()
        self._users: set[UUID] = set()
        self._waiters: list[asyncio.Future[None]] = []
        # Последняя уборка сеансов каждого места: пока она идёт, место не выдаётся.
        self._cleanups: dict[int, asyncio.Task[bool]] = {}

    @asynccontextmanager
    async def slot(self, user_id: UUID) -> AsyncIterator[SandboxSlot]:
        """Дождаться места, убедиться, что сеансов его роли нет, и держать до выхода."""
        number = await self._acquire(user_id)
        try:
            if not await self.terminate_sessions(number):
                raise SandboxUnavailableError
            yield _Slot(self, number, self._settings)
        finally:
            self._release(user_id, number)

    async def _acquire(self, user_id: UUID) -> int:
        """Номер свободного места; у одного сотрудника — не больше одного (§7.4)."""
        try:
            async with asyncio.timeout(self._settings.total_timeout_seconds):
                while True:
                    if len(self._retired) == self._settings.max_parallel:
                        raise SandboxUnavailableError
                    if user_id not in self._users and self._free:
                        number = min(self._free)
                        self._free.remove(number)
                        self._users.add(user_id)
                        return number
                    waiter = asyncio.get_running_loop().create_future()
                    self._waiters.append(waiter)
                    await waiter
        except TimeoutError:
            raise SandboxUnavailableError from None

    def _release(self, user_id: UUID, number: int) -> None:
        self._users.discard(user_id)
        cleanup = self._cleanups.get(number)
        if cleanup is None or cleanup.done():
            self._return(number)
        else:
            # Держатель места отменён, пока шла уборка его сеансов: она продолжается в
            # фоне и сняла бы сеанс следующей проверки, получи та это место сейчас.
            cleanup.add_done_callback(lambda _: self._return(number))
            self._wake()

    def _return(self, number: int) -> None:
        """Вернуть место в свободные."""
        if number not in self._retired:
            self._free.add(number)
        self._wake()

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def retire(self, number: int) -> None:
        """Вывести место из работы до перезапуска процесса (§7.4, [А-57])."""
        self._retired.add(number)
        # Номер места — не данные проверки; текста операторов в журнале нет.
        logger.error("sandbox slot retired", extra={"slot": number})

    def role(self, number: int) -> str:
        """Роль входа места."""
        return f"{self._settings.role_prefix}{number}"

    async def connect(self, role: str | None = None) -> Any:
        """Новое соединение под ролью места; без `role` — под управляющей ролью."""
        settings = self._settings
        try:
            return await asyncpg.connect(
                host=settings.host,
                port=settings.port,
                database=settings.database,
                user=role or settings.control_role,
                password=self._run_password if role else self._control_password,
                timeout=settings.connect_timeout_seconds,
                statement_cache_size=0,
            )
        except _FAILURES as error:
            raise SandboxUnavailableError from error

    async def closed(self, connection: Any, rollback: bool = False) -> bool:
        """Штатно закрыть соединение (с откатом транзакции); `False` — не удалось."""
        try:
            async with asyncio.timeout(self._settings.connect_timeout_seconds):
                if rollback:
                    await connection.execute("ROLLBACK")
                await connection.close()
            return True
        except _FAILURES:
            return False

    async def terminate_sessions(self, number: int) -> bool:
        """Под управляющей ролью завершить сеансы роли места; `True` — их не осталось.

        Ждёт не дольше `sql.sandbox.connect_timeout_seconds`.
        """
        control: Any = None
        try:
            # Один срок на всё: соединение, завершение сеансов и закрытие.
            async with asyncio.timeout(self._settings.connect_timeout_seconds):
                control = await self.connect()
                while await control.fetchval(_TERMINATE, self.role(number)):
                    await asyncio.sleep(_POLL_SECONDS)
                await control.close()
            return True
        except (SandboxUnavailableError, *_FAILURES):
            return False
        finally:
            if control is not None and not control.is_closed():
                control.terminate()

    async def force(self, number: int) -> None:
        """Принудительно завершить сеансы места — и тогда, когда сама задача отменена."""
        task = asyncio.ensure_future(self.terminate_sessions(number))
        self._cleanups[number] = task
        if not await asyncio.shield(task):
            # Следующая выдача места повторит завершение и без него место не выдаст.
            logger.warning("sandbox sessions not terminated", extra={"slot": number})


class _Slot:
    """Место песочницы: одна попытка — одно соединение и одна транзакция с откатом."""

    def __init__(
        self, sandbox: AsyncpgSqlSandbox, number: int, settings: SqlSandboxSettings
    ) -> None:
        self._sandbox = sandbox
        self._number = number
        self._settings = settings

    async def run(
        self, setup: Sequence[str], query: str, samples: Sequence[SampleTable] = ()
    ) -> SandboxRun:
        """Подготовить данные, выполнить запрос, прочитать результат и всё откатить."""
        sandbox = self._sandbox
        connection = await sandbox.connect(sandbox.role(self._number))
        forced = True
        try:
            outcome = await self._attempt(connection, setup, query, samples)
            forced = not await sandbox.closed(connection, rollback=True)
        except _TransactionEndedError:
            sandbox.retire(self._number)
            raise SandboxUnavailableError from None
        except _FAILURES as error:
            raise SandboxUnavailableError from error
        finally:
            # Попытка закончилась не откатом и штатным закрытием: отмена, предел чтения,
            # чужая транзакция, сбой — сеансов роли на сервере остаться не должно.
            if forced:
                connection.terminate()
                await sandbox.force(self._number)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _attempt(
        self,
        connection: Any,
        setup: Sequence[str],
        query: str,
        samples: Sequence[SampleTable] = (),
    ) -> SandboxRun | SandboxSetupError | SandboxQueryError:
        """Транзакция проверки; отказ СУБД возвращается, а не поднимается — сеанс цел."""
        schema = f"check_{uuid4().hex}"
        # До первого недоверенного оператора: значения этих типов приходят текстом СУБД.
        for name in _TEXT_TYPES:
            await connection.set_type_codec(
                name, schema="pg_catalog", encoder=tuple, decoder=tuple, format="tuple"
            )
        # Транзакция открывается средствами драйвера: без неё он не даёт читать курсором.
        await connection.transaction().start()
        await connection.execute(f"CREATE SCHEMA {_ident(schema)}")
        xact_id = await connection.fetchval(_XACT_ID)

        async def untrusted[T](action: Callable[[], Awaitable[T]]) -> T | _Rejected:
            await self._guard(connection, schema)
            try:
                value = await action()
            except (asyncpg.PostgresError, _ParseFailedError) as raised:
                error = raised.error if isinstance(raised, _ParseFailedError) else raised
                if connection.is_closed():
                    raise error from None
                if not connection.is_in_transaction():
                    raise _TransactionEndedError from None
                # Сообщение обрезается сразу при получении; только основное (поле `M`).
                return _Rejected(
                    (getattr(error, "message", None) or "")[: self._settings.error_max_chars],
                    getattr(error, "sqlstate", None),
                    isinstance(raised, _ParseFailedError),
                )
            # Одного состояния мало: `COMMIT AND CHAIN` оставляет соединение в транзакции,
            # но уже в другой.
            same = connection.is_in_transaction() and await connection.fetchval(_XACT_ID) == xact_id
            if not same:
                raise _TransactionEndedError
            return value

        for sample in samples:
            # Собственные операторы портала: имена — в кавычках, типы — после сверки,
            # значения — параметрами (§7.4, «Проверка на образце», шаг 4).
            await self._guard(connection, schema)
            try:
                await self._create_sample(connection, schema, sample)
            except asyncpg.PostgresError:
                if connection.is_closed():
                    raise
                return SandboxSetupError()
            except _UNSENDABLE:
                if connection.is_closed():
                    raise
                return SandboxSetupError()
        for statement in setup:
            if await untrusted(partial(self._prepare, connection, statement)) is not True:
                return SandboxSetupError()
        try:
            await self._guard(connection, schema)
            fixture = await self._fixture(connection, schema, {item.name for item in samples})
        except _TooLargeError:
            raise SandboxSetupError from None
        except asyncpg.PostgresError:
            if connection.is_closed():
                raise
            return SandboxSetupError()
        try:
            rows = await untrusted(partial(self._query, connection, query))
        except _TooLargeError:
            raise SandboxQueryError(_RESULT_TOO_LARGE, fixture) from None
        except _UnreadableError:
            raise SandboxQueryError(_RESULT_UNREADABLE, fixture) from None
        if isinstance(rows, _Rejected):
            return SandboxQueryError(rows.message, fixture, rows.sqlstate, rows.at_parse)
        await self._guard(connection, schema)
        result = await self._result(connection, rows) if rows.attributes else None
        return SandboxRun(fixture, result, rows.affected_rows)

    async def _guard(self, connection: Any, schema: str) -> None:
        """Выставить путь поиска и пределы времени заново: прежний оператор мог их снять."""
        settings = self._settings
        await connection.fetchrow(
            _GUARD,
            f"{_ident(schema)}, {_ident(_POSTGIS_SCHEMA)}",
            f"{settings.statement_timeout_seconds}s",
            f"{settings.lock_timeout_seconds}s",
        )

    @staticmethod
    async def _create_sample(connection: Any, schema: str, sample: SampleTable) -> None:
        """Создать и заполнить таблицу образца; строки с `NULL` в ключе пропускаются."""
        types = [sandbox_type(declared) for _, declared in sample.columns]
        names = [_ident(name) for name, _ in sample.columns]
        definition = [f"{name} {kind}" for name, kind in zip(names, types, strict=True)]
        if sample.primary_key:
            definition.append(f"PRIMARY KEY ({', '.join(_ident(k) for k in sample.primary_key)})")
        table = f"{_ident(schema)}.{_ident(sample.name)}"
        await connection.execute(f"CREATE TABLE {table} ({', '.join(definition)})")
        positions = [
            index for index, (name, _) in enumerate(sample.columns) if name in sample.primary_key
        ]
        rows = [
            list(row) for row in sample.rows if all(row[index] is not None for index in positions)
        ]
        if not rows:
            return
        values = ", ".join(
            f"(${position}::pg_catalog.text)::{kind}"
            for position, kind in enumerate(types, start=1)
        )
        await connection.executemany(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({values})", rows
        )

    @staticmethod
    async def _prepare(connection: Any, text: str) -> bool:
        """Выполнить оператор подготовки.

        `False` — он возвращает строки либо ждёт параметры и не исполнялся.
        """
        statement = await connection.prepare(text)
        if statement.get_attributes() or statement.get_parameters():
            return False
        # Предел в одну строку: оператор без столбцов тоже может вернуть сколько угодно строк.
        await statement.fetchrow()
        return True

    async def _query(self, connection: Any, text: str) -> _Rows | _Rejected:
        """Выполнить запрос; строки читаются по одной, не больше предела строк и байт.

        Запрос с параметрами (`$1`) не исполняется: значений для них нет, и это ошибка
        самого запроса, а не сбой песочницы. Результат, который драйвер не разобрал, —
        тоже: типы столбцов он выясняет при подготовке, значения — при чтении.
        """
        settings = self._settings
        try:
            try:
                statement = await connection.prepare(text)
            except asyncpg.PostgresError as error:
                raise _ParseFailedError(error) from None
            if statement.get_parameters():
                return _Rejected(_HAS_PARAMETERS)
            attributes = statement.get_attributes()
            if not attributes:
                await statement.fetchrow()
                return _Rows((), (), False, _count(statement.get_statusmsg()))
            records: list[Any] = []
            size = 0
            async for record in statement.cursor(prefetch=1):
                size += sum(_size(value) for value in record.values())
                if size > settings.result_max_bytes:
                    raise _TooLargeError
                records.append(record)
                if len(records) > settings.result_max_rows:
                    # Чтение прервано: числа строк сервер уже не сообщит.
                    return _Rows(attributes, records[: settings.result_max_rows], True, None)
        except _UNREADABLE as error:
            # Чтение оборвано на полпути.
            raise _UnreadableError from error
        return _Rows(attributes, records, False, len(records))

    async def _fixture(
        self, connection: Any, schema: str, samples: set[str] | frozenset[str] = frozenset()
    ) -> list[SandboxTable]:
        """Тестовые данные, как они легли в базу; таблицы и столбцы — из каталога.

        Таблицы образца (`samples`) считаются отдельно: предел байт у них не роняет блок —
        чтение этой и следующих таблиц образца останавливается, они получают `truncated`.
        """
        settings = self._settings
        sample_size = 0
        samples_stopped = False
        columns: dict[str, list[tuple[str, str, str]]] = {}
        for row in await connection.fetch(_TABLE_COLUMNS, schema):
            columns.setdefault(row["relname"], []).append(
                (row["attname"], row["type_schema"], row["typname"])
            )
        tables = []
        size = 0
        for table, described in columns.items():
            # Приведение к тексту, обрезка и знак «…» — на стороне СУБД.
            values = ", ".join(
                f"{_text_of(_ident(name), type_schema, type_name)} AS v{position}"
                for position, (name, type_schema, type_name) in enumerate(described)
            )
            cells = ", ".join(
                f"CASE WHEN pg_catalog.length(v{position}) > {settings.cell_max_chars}"
                f" THEN pg_catalog.left(v{position}, {settings.cell_max_chars}) || '{_ELLIPSIS}'"
                f" ELSE v{position} END"
                for position in range(len(described))
            )
            source = f"{_ident(schema)}.{_ident(table)} LIMIT {settings.fixture_max_rows + 1}"
            rows: list[list[str | None]] = []
            sample = table in samples
            cut = sample and samples_stopped
            if not cut:
                async for record in connection.cursor(
                    f"SELECT {cells} FROM (SELECT {values} FROM {source}) AS fixture", prefetch=1
                ):
                    taken = sum(_size(value) for value in record.values())
                    if sample:
                        sample_size += taken
                        if sample_size > settings.fixture_max_bytes:
                            samples_stopped = cut = True
                            break
                    else:
                        size += taken
                        if size > settings.fixture_max_bytes:
                            raise _TooLargeError
                    rows.append(list(record.values()))
            tables.append(
                SandboxTable(
                    table,
                    [name for name, _, _ in described],
                    rows[: settings.fixture_max_rows],
                    cut or len(rows) > settings.fixture_max_rows,
                )
            )
        return tables

    async def _result(self, connection: Any, rows: _Rows) -> SandboxTable:
        """Результат запроса текстом; ячейка обрезается сразу после чтения строки."""
        try:
            texts = await self._server_texts(connection, rows)
        except asyncpg.PostgresError:
            if connection.is_closed():
                raise
            # Сервер отверг возвращённое значение, транзакция прервана и спросить его
            # больше не о чем: значения показываются так, как их прочитал драйвер.
            texts = [
                [None if value is None else str(value) for value in record.values()]
                for record in rows.records
            ]
        limit = self._settings.cell_max_chars
        return SandboxTable(
            "",
            [attribute.name for attribute in rows.attributes],
            [
                [
                    text if text is None or len(text) <= limit else text[:limit] + _ELLIPSIS
                    for text in row
                ]
                for row in texts
            ],
            rows.truncated,
        )

    async def _server_texts(self, connection: Any, rows: _Rows) -> list[list[str | None]]:
        """Привести прочитанные значения к тексту на стороне СУБД — «как их отдаёт СУБД».

        Значения возвращаются серверу параметрами; имена типов — из каталога.
        """
        oids = [attribute.type.oid for attribute in rows.attributes]
        names = {
            row["oid"]: (row["nspname"], row["typname"])
            for row in await connection.fetch(_TYPE_NAMES, sorted(set(oids)))
        }

        def cell(position: int, oid: int) -> str:
            type_schema, type_name = names[oid]
            value = f"${position}::{_ident(type_schema)}.{_ident(type_name)}"
            text = _text_of(value, type_schema, type_name)
            return f"pg_catalog.left({text}, {self._settings.cell_max_chars + 1})"

        whole_row = "SELECT " + ", ".join(
            cell(position, oid) for position, oid in enumerate(oids, start=1)
        )
        texts: list[list[str | None]] = []
        for record in rows.records:
            values = [_sendable(value) for value in record.values()]
            try:
                texts.append(list((await connection.fetchrow(whole_row, *values)).values()))
            except _UNSENDABLE:
                if connection.is_closed():
                    raise
                # Драйвер не умеет вернуть серверу одно из значений (например, безымянную
                # запись): остальные ячейки строки спрашиваются по одной.
                texts.append(
                    [
                        await self._cell_text(connection, f"SELECT {cell(1, oid)}", value)
                        for oid, value in zip(oids, values, strict=True)
                    ]
                )
        return texts

    @staticmethod
    async def _cell_text(connection: Any, convert: str, value: object) -> str | None:
        """Текст одной ячейки; чего драйвер серверу вернуть не может — как он это прочитал."""
        try:
            text: str | None = await connection.fetchval(convert, value)
        except _UNSENDABLE:
            if connection.is_closed():
                raise
            return None if value is None else str(value)
        return text
