"""Песочница проверки SQL на настоящем PostgreSQL (docs/portal-api.md §7.4).

Порт принимает текст как есть, поэтому враждебный текст подаётся прямо в него. Роли и
база создаются в тестовом сервере так же, как их создаёт образ `sql-sandbox`; PostGIS,
пределы контейнера и журнал сервера здесь не проверяются — это проверки стенда.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from portal.core.settings import Settings, SqlSandboxSettings
from portal.tools.ports import (
    SandboxQueryError,
    SandboxRun,
    SandboxSetupError,
    SandboxUnavailableError,
)
from portal.tools.sandbox import AsyncpgSqlSandbox

pytestmark = pytest.mark.anyio

DATABASE = "sandbox_test"
SETUP = [
    "CREATE TABLE parcels (id int PRIMARY KEY, number text, area numeric, tags text[])",
    "INSERT INTO parcels VALUES (1, '77:01', 120.50, '{a,b}'), (2, '77:02', NULL, NULL)",
]
ROLES = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sandbox_ctl') THEN
        CREATE ROLE sandbox_run_1 LOGIN CONNECTION LIMIT 2;
        CREATE ROLE sandbox_run_2 LOGIN CONNECTION LIMIT 2;
        CREATE ROLE sandbox_ctl LOGIN;
        GRANT pg_signal_backend TO sandbox_ctl;
    END IF;
END $$
"""


async def _admin(settings: Settings, database: str = "postgres") -> Any:
    db = settings.database
    return await asyncpg.connect(host=db.host, port=db.port, user=db.user, database=database)


@pytest.fixture
async def config(settings: Settings) -> AsyncIterator[SqlSandboxSettings]:
    """Пустая база с ролями проверки и управляющей ролью — на каждый тест заново."""
    admin = await _admin(settings)
    try:
        await admin.execute(ROLES)
        await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {DATABASE}")
        await admin.execute(
            f"REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;"
            f"GRANT CONNECT, CREATE ON DATABASE {DATABASE} TO sandbox_run_1, sandbox_run_2;"
            f"GRANT CONNECT ON DATABASE {DATABASE} TO sandbox_ctl"
        )
    finally:
        await admin.close()
    yield settings.sql.sandbox.model_copy(
        update={
            "enabled": True,
            "host": settings.database.host,
            "port": settings.database.port,
            "database": DATABASE,
            "connect_timeout_seconds": 3,
            "total_timeout_seconds": 1,
        }
    )


def _sandbox(config: SqlSandboxSettings, **update: Any) -> AsyncpgSqlSandbox:
    return AsyncpgSqlSandbox(config.model_copy(update=update), "run-password", "ctl-password")


async def _run(sandbox: AsyncpgSqlSandbox, setup: list[str], query: str) -> SandboxRun:
    async with sandbox.slot(uuid4()) as slot:
        return await slot.run(setup, query)


async def _leftovers(settings: Settings) -> tuple[int, int]:
    """Сколько в базе осталось сеансов ролей проверки и объектов проверок."""
    admin = await _admin(settings, DATABASE)
    try:
        sessions = await admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE usename LIKE 'sandbox_run_%'"
        )
        objects = await admin.fetchval(
            "SELECT (SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'check_%')"
            " + (SELECT count(*) FROM pg_class WHERE relname IN ('parcels', 'leak'))"
            " + (SELECT count(*) FROM pg_largeobject_metadata)"
        )
        return sessions, objects
    finally:
        await admin.close()


async def _no_sessions(settings: Settings) -> None:
    """Сеанс закрытого соединения исчезает с сервера не мгновенно."""
    async with asyncio.timeout(5):
        while (await _leftovers(settings))[0]:
            await asyncio.sleep(0.05)


async def test_fixture_and_result_come_as_database_text_and_nothing_is_kept(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    sandbox = _sandbox(config, cell_max_chars=5)
    query = (
        "SELECT id, area, tags, area IS NULL AS empty, lo_create(0) > 0 AS lo,"
        " repeat('я', 9) AS long FROM parcels ORDER BY id"
    )
    run = await _run(sandbox, SETUP, query)

    table = run.fixture[0]
    assert (table.table, list(table.columns)) == ("parcels", ["id", "number", "area", "tags"])
    assert [list(row) for row in table.rows] == [
        ["1", "77:01", "120.5…", "{a,b}"],
        ["2", "77:02", None, None],
    ]
    assert run.result is not None and run.result.table == ""
    assert list(run.result.columns) == ["id", "area", "tags", "empty", "lo", "long"]
    # Значения — текст СУБД, а не представление драйвера; длинная ячейка обрезана со знаком «…».
    assert [list(row) for row in run.result.rows] == [
        ["1", "120.5…", "{a,b}", "false", "true", "яяяяя…"],
        ["2", None, None, "true", "true", "яяяяя…"],
    ]
    assert (run.result.truncated, table.truncated) == (False, False)

    changed = await _run(sandbox, SETUP, "UPDATE parcels SET area = 1 WHERE id > 0")
    assert (changed.result, changed.affected_rows) == (None, 2)
    returned = await _run(sandbox, SETUP, "DELETE FROM parcels WHERE id = 1 RETURNING id")
    assert returned.result is not None and returned.affected_rows == 1
    assert [list(row) for row in returned.result.rows] == [["1"]]

    # Транзакция всегда откатывается: ни таблиц, ни схемы проверки, ни large objects.
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


async def test_text_of_several_commands_is_rejected_by_the_server(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    sandbox = _sandbox(config)
    with pytest.raises(SandboxSetupError):
        await _run(sandbox, ["CREATE TABLE leak (id int); COMMIT"], "SELECT 1")
    with pytest.raises(SandboxQueryError) as failure:
        await _run(sandbox, SETUP, "SELECT 1; CREATE TABLE leak (id int)")
    assert "multiple commands" in failure.value.message
    # Оператор подготовки, возвращающий строки, не исполняется вовсе.
    with pytest.raises(SandboxSetupError):
        await _run(sandbox, ["SELECT lo_create(0) FROM generate_series(1, 100000000)"], "SELECT 1")
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


@pytest.mark.parametrize("statement", ["COMMIT", "COMMIT AND CHAIN", "ROLLBACK"])
@pytest.mark.parametrize("as_setup", [True, False])
async def test_statement_that_ends_the_transaction_retires_the_slot(
    settings: Settings, config: SqlSandboxSettings, statement: str, as_setup: bool
) -> None:
    sandbox = _sandbox(config, max_parallel=1)
    with pytest.raises(SandboxUnavailableError):
        await _run(
            sandbox, [statement] if as_setup else SETUP, "SELECT 1" if as_setup else statement
        )
    await _no_sessions(settings)
    # Место выведено из работы: рабочих мест не осталось, отказ сразу, без ожидания.
    async with asyncio.timeout(0.5):
        with pytest.raises(SandboxUnavailableError):
            await _run(sandbox, SETUP, "SELECT 1")


async def test_reading_stops_at_the_limits(settings: Settings, config: SqlSandboxSettings) -> None:
    sandbox = _sandbox(
        config, result_max_bytes=10_000, fixture_max_bytes=10_000, error_max_chars=40
    )

    endless = await _run(sandbox, SETUP, "SELECT generate_series(1, 100000000000) AS n")
    assert endless.result is not None and endless.result.truncated
    assert len(endless.result.rows) == config.result_max_rows
    # Обрезанный результат изменяющего запроса: числа строк сервер не сообщил.
    partial = await _run(
        sandbox,
        SETUP,
        "INSERT INTO parcels SELECT n FROM generate_series(3, 500) AS n RETURNING id",
    )
    assert partial.result is not None and partial.result.truncated
    assert partial.affected_rows is None

    with pytest.raises(SandboxQueryError) as huge:
        await _run(sandbox, SETUP, "SELECT repeat('x', 1000000)")
    assert huge.value.message == "result is too large"
    await _no_sessions(settings)  # сеанс завершён принудительно

    many_rows = "INSERT INTO parcels SELECT n, 'x' FROM generate_series(3, 40) AS n"
    grown = await _run(sandbox, [*SETUP, many_rows], "SELECT 1")
    assert len(grown.fixture[0].rows) == config.fixture_max_rows and grown.fixture[0].truncated
    wide = "CREATE TABLE wide (" + ", ".join(
        f"c{n} text DEFAULT repeat('x', 200)" for n in range(60)
    )
    with pytest.raises(SandboxSetupError):
        await _run(sandbox, [wide + ")", "INSERT INTO wide DEFAULT VALUES"], "SELECT 1")

    with pytest.raises(SandboxQueryError) as long_error:
        await _run(sandbox, SETUP, "SELECT ('x' || repeat('y', 100000))::int")
    assert len(long_error.value.message) == 40
    await _no_sessions(settings)


async def test_statement_timeout_and_forced_termination_on_cancel(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    quick = _sandbox(config, statement_timeout_seconds=1)
    with pytest.raises(SandboxQueryError) as slow:
        await _run(quick, SETUP, "SELECT pg_sleep(30)")
    assert "statement timeout" in slow.value.message

    # Предел оператора не сработал (как если бы запрос его снял): общий срок держит
    # вызывающая сторона, отмена задачи завершает сеанс на сервере.
    patient = _sandbox(config, statement_timeout_seconds=600)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await _run(patient, SETUP, "SELECT pg_sleep(600)")
    assert await _leftovers(settings) == (0, 0)
    # Место после обрыва снова выдаётся и работает.
    assert (await _run(patient, SETUP, "SELECT 1")).result is not None


async def test_cleanup_left_in_background_does_not_end_the_next_check(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    """Вторая отмена застаёт уборку сеансов: место не выдаётся, пока она не закончилась."""
    sandbox = _sandbox(config, max_parallel=1, statement_timeout_seconds=600)
    first = asyncio.create_task(_run(sandbox, SETUP, "SELECT pg_sleep(600)"))
    sessions = "SELECT count(*) FROM pg_stat_activity WHERE usename = 'sandbox_run_1'"
    admin = await _admin(settings, DATABASE)
    try:
        async with asyncio.timeout(5):
            while not await admin.fetchval(sessions + " AND query LIKE 'SELECT pg_sleep%'"):
                await asyncio.sleep(0.05)
            second = asyncio.create_task(_run(sandbox, SETUP, "SELECT pg_sleep(0.5)"))
            first.cancel()
            # Уборка сняла сеанс и ждёт, чтобы убедиться: в этот промежуток место, выданное
            # сразу, прошло бы проверку чистоты, а следующий её круг снял бы новый сеанс.
            while await admin.fetchval(sessions):
                await asyncio.sleep(0)
    finally:
        await admin.close()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert (await second).result is not None
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


async def test_concurrent_checks_do_not_see_each_other(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    sandbox = _sandbox(config, statement_timeout_seconds=600)
    first = asyncio.create_task(_run(sandbox, SETUP, "SELECT pg_sleep(600)"))
    admin = await _admin(settings, DATABASE)
    try:
        async with asyncio.timeout(5):
            while not await admin.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep%'"
            ):
                await asyncio.sleep(0.05)
    finally:
        await admin.close()

    spy = (
        "SELECT (SELECT count(*) FROM pg_class WHERE relname = 'parcels') AS tables,"
        " (SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'check_%') AS schemas,"
        " (SELECT string_agg(query, '') FROM pg_stat_activity"
        "   WHERE usename = 'sandbox_run_1') AS query"
    )
    seen = await _run(sandbox, [], spy)
    assert seen.result is not None
    # Своя схема проверки видна, чужая — нет; текст чужого запроса скрыт.
    assert [list(row) for row in seen.result.rows] == [["0", "1", "<insufficient privilege>"]]
    with pytest.raises(SandboxQueryError):
        await _run(
            sandbox,
            [],
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE usename = 'sandbox_run_1'",
        )
    assert not first.done()  # чужой сеанс не снят
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await _no_sessions(settings)


async def test_one_slot_per_user_and_limit_of_parallel_checks(config: SqlSandboxSettings) -> None:
    sandbox = _sandbox(config, max_parallel=2)
    user, other, third = UUID(int=1), UUID(int=2), UUID(int=3)
    async with sandbox.slot(user) as held:
        # Вторая проверка того же сотрудника ждёт первую и за срок ожидания места не получает.
        with pytest.raises(SandboxUnavailableError):
            async with sandbox.slot(user):
                pass
        async with sandbox.slot(other) as second:
            assert second is not held
            with pytest.raises(SandboxUnavailableError):
                async with sandbox.slot(third):
                    pass
        waiting = asyncio.create_task(_run(sandbox, [], "SELECT 1"))
        assert (await waiting).result is not None  # место освободилось — выдано
    async with sandbox.slot(user) as again:
        assert (await again.run([], "SELECT 1")).result is not None


async def test_unreachable_sandbox_is_unavailable_and_recovers(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    down = _sandbox(config, port=1)
    with pytest.raises(SandboxUnavailableError):
        await _run(down, SETUP, "SELECT 1")

    # Сеанс роли, оставшийся с прошлого раза, завершается перед выдачей места.
    db = settings.database
    stale = await asyncpg.connect(
        host=db.host, port=db.port, user="sandbox_run_1", database=DATABASE
    )
    assert (await _run(_sandbox(config), SETUP, "SELECT 1")).result is not None
    await asyncio.sleep(0.1)
    assert stale.is_closed()


async def test_values_are_shown_as_the_database_prints_them(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    """Типы даты и интервала не проходят через типы Python: ни потерь, ни отказов (§7.4)."""
    sandbox = _sandbox(config)

    async def row(query: str) -> list[str | None]:
        run = await _run(sandbox, [], query)
        assert run.result is not None
        return list(run.result.rows[0])

    assert await row(
        "SELECT interval '1 month', interval '1 year 2 mons 3 days',"
        " age(date '2024-03-01', date '2020-01-15')"
    ) == ["1 mon", "1 year 2 mons 3 days", "4 years 1 mon 17 days"]
    # Вне диапазона типов Python: до нашей эры, год больше 9999, бесконечность.
    assert await row(
        "SELECT date '0044-03-15 BC', date '20000-01-01', timestamp '294276-01-01',"
        " date 'infinity', timestamptz '2024-01-01 10:00+00' IS NOT NULL, time '10:00'"
    ) == ["0044-03-15 BC", "20000-01-01", "294276-01-01 00:00:00", "infinity", "true", "10:00:00"]
    assert await row(
        "SELECT ARRAY[date '0044-03-15 BC'], daterange('2024-01-01', '20000-01-01'),"
        " ARRAY[interval '1 mon']"
    ) == ['{"0044-03-15 BC"}', "[2024-01-01,20000-01-01)", '{"1 mon"}']
    # Безымянную запись драйвер серверу не вернёт: она одна показана, как её прочитал
    # драйвер, а соседние ячейки остаются текстом СУБД.
    assert await row("SELECT ROW(1, 'a'), ARRAY[1, 2], true") == ["(1, 'a')", "{1,2}", "true"]
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


async def test_table_rows_and_their_arrays_are_shown_as_the_database_prints_them(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    """Строка таблицы и массив строк — текст СУБД, а не записи драйвера (§7.4)."""
    sandbox = _sandbox(config)
    dated = [
        *SETUP,
        "CREATE TABLE visits (day date, parcel parcels)",
        "INSERT INTO visits VALUES ('0044-03-15 BC', NULL), (NULL, ROW(1, 'x', 1.5, '{q}'))",
    ]

    async def cells(query: str) -> list[list[str | None]]:
        run = await _run(sandbox, dated, query)
        assert run.result is not None
        return [list(row) for row in run.result.rows]

    assert await cells("SELECT p FROM parcels p ORDER BY id") == [
        ['(1,77:01,120.50,"{a,b}")'],
        ["(2,77:02,,)"],
    ]
    assert await cells("SELECT array_agg(p ORDER BY id), 1 FROM parcels p") == [
        ['{"(1,77:01,120.50,\\"{a,b}\\")","(2,77:02,,)"}', "1"]
    ]
    assert await cells("SELECT array_agg(v) FROM visits v") == [
        ['{"(\\"0044-03-15 BC\\",)","(,\\"(1,x,1.5,{q})\\")"}']
    ]
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


@pytest.mark.parametrize(
    "query",
    [
        # Столбец псевдотипа системного каталога: драйвер не выясняет его тип.
        "SELECT most_common_vals FROM pg_stats LIMIT 1",
        # Составной тип с полем, которое драйвер читает только текстом (как `geometry`).
        "SELECT d FROM docs d",
        "SELECT array_agg(d) FROM docs d",
    ],
)
async def test_result_the_driver_cannot_decode_is_a_query_error(
    settings: Settings, config: SqlSandboxSettings, query: str
) -> None:
    """Отказ драйвера на типе результата — ошибка запроса, а не «песочница недоступна»."""
    sandbox = _sandbox(config)
    setup = ["CREATE TABLE docs (id int, body tsvector)", "INSERT INTO docs VALUES (1, 'a b')"]
    with pytest.raises(SandboxQueryError) as unreadable:
        await _run(sandbox, setup, query)
    assert unreadable.value.message == "result cannot be read"
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


async def test_catalog_row_with_types_the_driver_cannot_send_back_is_still_read(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    """Значение, которое драйвер не вернёт серверу, не роняет чтение остальных ячеек."""
    run = await _run(
        _sandbox(config),
        SETUP,
        "SELECT relname, relpartbound, relkind FROM pg_class WHERE relname = 'parcels'",
    )
    assert run.result is not None
    assert [list(row) for row in run.result.rows] == [["parcels", None, "r"]]
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


async def test_statement_with_parameters_is_refused_as_a_query_error(
    settings: Settings, config: SqlSandboxSettings
) -> None:
    """Запрос с `$1` — ошибка запроса, а не «песочница недоступна»: значений для него нет."""
    sandbox = _sandbox(config)
    for query in ("SELECT * FROM parcels WHERE id = $1", "UPDATE parcels SET area = $1"):
        with pytest.raises(SandboxQueryError) as refused:
            await _run(sandbox, SETUP, query)
        assert refused.value.message == "query has parameters"
    with pytest.raises(SandboxSetupError):
        await _run(sandbox, [SETUP[0], "INSERT INTO parcels (id) VALUES ($1)"], "SELECT 1")
    # Ошибка запроса снимается ошибкой сервера так же, как раньше.
    with pytest.raises(SandboxQueryError) as failed:
        await _run(sandbox, SETUP, "SELECT 1 / (id - 1) FROM parcels ORDER BY id DESC")
    assert failed.value.message == "division by zero"
    # Данные, на которых запрос не выполнился, приходят вместе с ошибкой.
    assert [(table.table, len(table.rows)) for table in failed.value.fixture] == [("parcels", 2)]
    assert (await _run(sandbox, SETUP, "SELECT 1")).result is not None
    await _no_sessions(settings)
    assert await _leftovers(settings) == (0, 0)


async def test_value_the_driver_cannot_read_is_a_query_error(
    settings: Settings, config: SqlSandboxSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Из порта выходят только три его исключения — и когда значение не разобрал драйвер."""

    def undecodable(value: object) -> int:
        raise OverflowError("date value out of range")

    monkeypatch.setattr("portal.tools.sandbox._size", undecodable)
    sandbox = _sandbox(config)
    with pytest.raises(SandboxQueryError) as unreadable:
        await _run(sandbox, [], "SELECT 1")
    assert unreadable.value.message == "result cannot be read"
    await _no_sessions(settings)  # чтение оборвано на полпути: сеанс завершён принудительно
