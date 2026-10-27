"""Адаптер баз заказчика на настоящем PostgreSQL (docs/portal-api.md §7.6.5–7.6.8).

Роли с разными правами создаёт фикстура в тестовом сервере (`CLAUDE.md`, пайплайн «Код»,
правило 21); PostGIS здесь нет — геометрия проверяется на стенде
(`portal/dev/stand/test_customer_db.py`). Тестовый сервер слушает только сокет: имя
подключения разрешается подменой в адрес из TEST-NET, а соединение на этот адрес ведётся
в сокет (§13.3) — проверка адреса при этом не обходится.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from portal.core.settings import DbSettings, Settings
from portal.databases.customer import AsyncpgCustomerDatabase, sandbox_type
from portal.databases.hosts import HostPolicy
from portal.databases.ports import (
    DbRightsExcessError,
    DbTarget,
    DbUnavailableError,
    SampleRequest,
)

pytestmark = pytest.mark.anyio

DATABASE = "customer_test"
SCHEMA = """
CREATE SCHEMA shop;
CREATE TABLE shop.items (id serial PRIMARY KEY, name text, note text);
INSERT INTO shop.items (name, note) VALUES ('Первый', repeat('x', 100)), ('Второй', NULL);
CREATE DOMAIN shop.qty AS integer CHECK (VALUE > 0);
CREATE TABLE shop.orders (
    item_id integer REFERENCES shop.items (id), qty shop.qty, area numeric(12,2), made date
);
CREATE VIEW shop.mode AS
    SELECT pg_catalog.current_setting('transaction_read_only') AS read_only,
           pg_catalog.current_setting('transaction_isolation') AS isolation;
CREATE VIEW shop.slow AS SELECT 1 AS id, pg_catalog.pg_sleep(3)::text AS waited;
CREATE FUNCTION shop.recalc() RETURNS void LANGUAGE sql SECURITY DEFINER
    AS 'UPDATE shop.items SET note = NULL';
REVOKE EXECUTE ON FUNCTION shop.recalc() FROM PUBLIC;
"""
# Роль → лишнее право (кроме чтения схемы `shop`, оно есть у всех) и ожидаемый вид находки.
ROLES: dict[str, tuple[str, str | None]] = {
    "reader": ("", None),
    "insert": ("GRANT INSERT ON shop.items TO {r}", "write"),
    "update_column": ("GRANT UPDATE (note) ON shop.items TO {r}", "write"),
    "seq_update": ("GRANT UPDATE ON SEQUENCE shop.items_id_seq TO {r}", "write"),
    "seq_usage": ("GRANT USAGE ON SEQUENCE shop.items_id_seq TO {r}", "write"),
    "table_owner": (
        "CREATE TABLE shop.owned_{r} (id int); ALTER TABLE shop.owned_{r} OWNER TO {r}",
        "owner",
    ),
    "schema_owner": ("CREATE SCHEMA owned_{r} AUTHORIZATION {r}", "owner"),
    "create_schema": ("GRANT CREATE ON SCHEMA shop TO {r}", "create"),
    "create_db": (f"GRANT CREATE ON DATABASE {DATABASE} TO {{r}}", "create"),
    "write_all": ("GRANT pg_write_all_data TO {r}", "predefined_role"),
    "createrole": ("ALTER ROLE {r} CREATEROLE", "createrole"),
    "superuser": ("ALTER ROLE {r} SUPERUSER", "superuser"),
    "noinherit": (
        "ALTER ROLE {r} NOINHERIT; CREATE ROLE {r}_g NOLOGIN;"
        " GRANT INSERT ON shop.items TO {r}_g; GRANT {r}_g TO {r}",
        "write",
    ),
    "two_steps": (
        "CREATE ROLE {r}_g1 NOLOGIN; CREATE ROLE {r}_g2 NOLOGIN;"
        " GRANT INSERT ON shop.items TO {r}_g2; GRANT {r}_g2 TO {r}_g1; GRANT {r}_g1 TO {r}",
        "write",
    ),
    "definer": ("GRANT EXECUTE ON FUNCTION shop.recalc() TO {r}", None),
    "grant_table": ("GRANT SELECT ON shop.items TO {r} WITH GRANT OPTION", "grant_option"),
    "grant_column": ("GRANT SELECT (name) ON shop.items TO {r} WITH GRANT OPTION", "grant_option"),
    "grant_schema": ("GRANT USAGE ON SCHEMA shop TO {r} WITH GRANT OPTION", "grant_option"),
    "grant_group": (
        "ALTER ROLE {r} NOINHERIT; CREATE ROLE {r}_g NOLOGIN;"
        " GRANT SELECT ON shop.items TO {r}_g WITH GRANT OPTION; GRANT {r}_g TO {r}",
        "grant_option",
    ),
    "admin_direct": (
        "CREATE ROLE {r}_g NOLOGIN; GRANT {r}_g TO {r} WITH ADMIN OPTION",
        "admin_option",
    ),
    "admin_group": (
        "CREATE ROLE {r}_g1 NOLOGIN; CREATE ROLE {r}_g2 NOLOGIN;"
        " GRANT {r}_g2 TO {r}_g1 WITH ADMIN OPTION; GRANT {r}_g1 TO {r}",
        "admin_option",
    ),
    "alter_system": ("GRANT ALTER SYSTEM ON PARAMETER work_mem TO {r}", "server_settings"),
    # log_statement — параметр уровня суперпользователя.
    "set_parameter": ("GRANT SET ON PARAMETER log_statement TO {r}", "server_settings"),
    "set_group": (
        "CREATE ROLE {r}_g NOLOGIN; GRANT SET ON PARAMETER log_min_duration_statement TO {r}_g;"
        " GRANT {r}_g TO {r}",
        "server_settings",
    ),
    # `lower(text)` открыта `PUBLIC` изначально — выданное ей не находка (запрос 11).
    "files": (
        "GRANT EXECUTE ON FUNCTION pg_catalog.lo_export(oid, text) TO {r};"
        " GRANT EXECUTE ON FUNCTION pg_catalog.lower(text) TO {r}",
        "catalog_function",
    ),
    "files_group": (
        "CREATE ROLE {r}_g NOLOGIN;"
        " GRANT EXECUTE ON FUNCTION pg_catalog.lo_export(oid, text) TO {r}_g; GRANT {r}_g TO {r}",
        "catalog_function",
    ),
    "lower": ("GRANT EXECUTE ON FUNCTION pg_catalog.lower(text) TO {r}", None),
    # `pg_ls_logdir()` открыт `pg_monitor` при `initdb`, а саму роль разрешает запрос 5.
    "monitor": ("GRANT pg_monitor TO {r}", None),
    "monitor_direct": (
        "GRANT pg_monitor TO {r}; GRANT EXECUTE ON FUNCTION pg_catalog.pg_ls_logdir() TO {r}",
        "catalog_function",
    ),
    "lo_update": (
        "DO $$ BEGIN EXECUTE pg_catalog.format("
        "'GRANT UPDATE ON LARGE OBJECT %s TO {r}', pg_catalog.lo_create(0)); END $$",
        "large_object",
    ),
    "lo_select": (
        "DO $$ BEGIN EXECUTE pg_catalog.format("
        "'GRANT SELECT ON LARGE OBJECT %s TO {r}', pg_catalog.lo_create(0)); END $$",
        None,
    ),
    "lo_compat": ("ALTER ROLE {r} SET lo_compat_privileges = on", "lo_compat_privileges"),
}
# Объекты находок запросов 11–13 (`total` — 1).
FILE_OBJECTS = {
    "files": ("catalog_function", ("EXECUTE",), [("function", "lo_export(oid,text)")]),
    "files_group": ("catalog_function", ("EXECUTE",), [("function", "lo_export(oid,text)")]),
    "lo_compat": ("lo_compat_privileges", (), [("parameter", "lo_compat_privileges")]),
}
# Права на параметр у находки `server_settings`.
PARAMETER_PRIVILEGES = {
    "alter_system": ("ALTER SYSTEM",),
    "set_parameter": ("SET",),
    "set_group": ("SET",),
}
ADDRESS = "192.0.2.10"


def _role(name: str) -> str:
    return f"cust_{name}"


async def _admin(settings: Settings, database: str = "postgres") -> Any:
    db = settings.database
    return await asyncpg.connect(host=db.host, port=db.port, user=db.user, database=database)


@pytest.fixture(scope="module")
async def prepared(settings: Settings) -> AsyncIterator[None]:
    """База «заказчика» и роли — один раз на модуль."""
    admin = await _admin(settings)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        roles = await admin.fetch("SELECT rolname FROM pg_roles WHERE rolname LIKE 'cust\\_%'")
        for row in roles:
            await admin.execute(f'DROP ROLE "{row["rolname"]}"')
        await admin.execute(f"CREATE DATABASE {DATABASE}")
    finally:
        await admin.close()
    admin = await _admin(settings, DATABASE)
    try:
        await admin.execute(SCHEMA)
        for name, (grant, _) in ROLES.items():
            role = _role(name)
            await admin.execute(f"CREATE ROLE {role} LOGIN")
            await admin.execute(
                f"GRANT USAGE ON SCHEMA shop TO {role};"
                f" GRANT SELECT ON ALL TABLES IN SCHEMA shop TO {role}"
            )
            if grant:
                await admin.execute(grant.format(r=role))
        await admin.execute("CREATE ROLE cust_late LOGIN; GRANT USAGE ON SCHEMA shop TO cust_late")
    finally:
        await admin.close()
    yield


@pytest.fixture(autouse=True)
def socket_route(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Соединение на проверенный адрес `ADDRESS` ведётся в сокет тестового сервера.

    Соединения фикстур (сразу в сокет) проходят как есть.
    """
    connect = asyncpg.connect
    hosts: list[str] = []

    async def routed(*args: Any, host: str, **kwargs: Any) -> Any:
        hosts.append(host)
        return await connect(
            *args, host=settings.database.host if host == ADDRESS else host, **kwargs
        )

    monkeypatch.setattr(asyncpg, "connect", routed)
    return hosts


async def _resolve(host: str, port: int) -> list[str]:
    return [ADDRESS]


def _target(settings: Settings, role: str) -> DbTarget:
    db = settings.database
    return DbTarget(
        uuid4(),
        "db.customer.test",
        db.port,
        DATABASE,
        role,
        "unused",
        "disable",
        ("shop", "public"),
    )


def _adapter(settings: Settings, **update: Any) -> AsyncpgCustomerDatabase:
    config: DbSettings = settings.db.model_copy(update={"enabled": True, **update})
    policy = HostPolicy.of(config.forbidden_hosts, config.allowed_internal_hosts, ())
    return AsyncpgCustomerDatabase(config, policy, _resolve)


@pytest.mark.parametrize("name", list(ROLES))
async def test_rights_of_each_role(settings: Settings, prepared: None, name: str) -> None:
    report = await _adapter(settings).check(_target(settings, _role(name)))
    expected = ROLES[name][1]
    kinds = [item.kind for item in report.findings]
    if expected is None:
        assert kinds == []
    else:
        assert expected in kinds
    if name in PARAMETER_PRIVILEGES:
        (found,) = [item for item in report.findings if item.kind == "server_settings"]
        assert found.privileges == PARAMETER_PRIVILEGES[name]
        assert all(kind == "parameter" for kind, _ in found.objects)
    if name in FILE_OBJECTS:
        kind, privileges, objects = FILE_OBJECTS[name]
        (found,) = [item for item in report.findings if item.kind == kind]
        assert (found.privileges, list(found.objects), found.total) == (privileges, objects, 1)
    if name == "lo_update":
        (found,) = [item for item in report.findings if item.kind == "large_object"]
        assert found.privileges == ("UPDATE",)
        assert [kind for kind, _ in found.objects] == ["large object"]
    definers = [name for item in report.warnings for _, name in item.objects]
    assert definers == (["shop.recalc"] if name == "definer" else [])


async def test_parameter_right_of_public_is_found_for_every_role(
    settings: Settings, prepared: None
) -> None:
    admin = await _admin(settings, DATABASE)
    try:
        await admin.execute("GRANT SET ON PARAMETER log_temp_files TO PUBLIC")
        report = await _adapter(settings).check(_target(settings, _role("reader")))
    finally:
        await admin.execute("REVOKE SET ON PARAMETER log_temp_files FROM PUBLIC")
        await admin.close()
    (found,) = [item for item in report.findings if item.kind == "server_settings"]
    assert (found.privileges, found.objects) == (("SET",), (("parameter", "log_temp_files"),))


@pytest.mark.parametrize(
    ("grant", "revoke", "kind"),
    [
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.lo_export(oid, text) TO PUBLIC",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.lo_export(oid, text) FROM PUBLIC",
            "catalog_function",
        ),
        (
            "GRANT UPDATE ON LARGE OBJECT {oid} TO PUBLIC",
            "REVOKE UPDATE ON LARGE OBJECT {oid} FROM PUBLIC",
            "large_object",
        ),
    ],
)
async def test_file_rights_of_public_are_found_for_every_role(
    settings: Settings, prepared: None, grant: str, revoke: str, kind: str
) -> None:
    """Выданное `PUBLIC` есть у любой роли — находка и у роли только с `SELECT` (§7.6.6)."""
    admin = await _admin(settings, DATABASE)
    try:
        oid = await admin.fetchval("SELECT pg_catalog.lo_create(0)")
        await admin.execute(grant.format(oid=oid))
        report = await _adapter(settings).check(_target(settings, _role("reader")))
    finally:
        await admin.execute(revoke.format(oid=oid))
        await admin.execute("SELECT pg_catalog.lo_unlink($1)", oid)
        await admin.close()
    assert [item.kind for item in report.findings] == [kind]


# Как функции расширений (`dblink_connect_u`, `pg_surgery`): закрыта для `PUBLIC` скриптом
# расширения — начальные права записаны в `pg_init_privs` (запрос 11, решение 44).
EXTENSION_CONTROL = "default_version = '1.0'\nrelocatable = false\nschema = public\n"
EXTENSION_SCRIPT = """
CREATE FUNCTION @extschema@.ext_danger() RETURNS int LANGUAGE sql AS 'SELECT 1';
REVOKE EXECUTE ON FUNCTION @extschema@.ext_danger() FROM PUBLIC;
"""


async def test_function_closed_by_extension_is_found_in_any_schema(
    settings: Settings, prepared: None, tmp_path: Path
) -> None:
    """`EXECUTE` на функцию расширения в `public`, закрытую при `CREATE EXTENSION`, — находка
    с именем со схемой; своя функция с отозванным у `PUBLIC` правом — не находка (§7.6.6)."""
    admin = await _admin(settings, DATABASE)
    try:
        if await admin.fetchval("SELECT current_setting('server_version_num')::int") < 180000:
            pytest.skip("extension_control_path есть только в PostgreSQL 18+")
        folder = tmp_path / "extension"
        folder.mkdir()
        (folder / "probe_closed.control").write_text(EXTENSION_CONTROL)
        (folder / "probe_closed--1.0.sql").write_text(EXTENSION_SCRIPT)
        await admin.execute(f"SET extension_control_path = '{tmp_path}:$system'")
        await admin.execute("CREATE EXTENSION probe_closed")
        await admin.execute(
            "CREATE FUNCTION public.own_closed() RETURNS int LANGUAGE sql AS 'SELECT 1';"
            " REVOKE EXECUTE ON FUNCTION public.own_closed() FROM PUBLIC;"
            " GRANT EXECUTE ON FUNCTION public.own_closed() TO cust_reader"
        )
        clean = await _adapter(settings).check(_target(settings, _role("reader")))
        await admin.execute("GRANT EXECUTE ON FUNCTION public.ext_danger() TO cust_reader")
        report = await _adapter(settings).check(_target(settings, _role("reader")))
    finally:
        await admin.execute("DROP EXTENSION IF EXISTS probe_closed")
        await admin.execute("DROP FUNCTION IF EXISTS public.own_closed()")
        await admin.close()
    assert clean.findings == []
    (found,) = report.findings
    assert (found.kind, found.objects) == (
        "catalog_function",
        (("function", "public.ext_danger()"),),
    )


async def test_maintain_is_found_on_postgresql_17(settings: Settings, prepared: None) -> None:
    admin = await _admin(settings, DATABASE)
    try:
        if await admin.fetchval("SELECT current_setting('server_version_num')::int") < 170000:
            pytest.skip("MAINTAIN есть только в PostgreSQL 17+ (проверить на ВМ)")
        await admin.execute("GRANT MAINTAIN ON shop.items TO cust_reader")
        report = await _adapter(settings).check(_target(settings, _role("reader")))
    finally:
        await admin.execute("REVOKE ALL ON shop.items FROM cust_reader")
        await admin.execute("GRANT SELECT ON shop.items TO cust_reader")
        await admin.close()
    assert [item.kind for item in report.findings] == ["maintain"]


class _OldServer:
    """Соединение с сервером PostgreSQL 14: запоминает операторы, каталоги пусты."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.statements: list[str] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.statements.append(query)
        return self.version if "server_version_num" in query else False

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.statements.append(query)
        return []


@pytest.mark.parametrize(("version", "sent"), [(140000, False), (150000, True)])
async def test_parameter_catalog_only_from_postgresql_15(
    settings: Settings, version: int, sent: bool
) -> None:
    """На PostgreSQL ≤ 14 нет `pg_parameter_acl`: запрос 10 не идёт, у запроса 8 нет ветви."""
    connection = _OldServer(version)
    report = await _adapter(settings)._rights(connection)
    assert report.findings == []
    with_catalog = [query for query in connection.statements if "pg_parameter_acl" in query]
    grant_option = [query for query in connection.statements if "is_grantable" in query]
    assert len(grant_option) == 1
    assert ("pg_parameter_acl" in grant_option[0]) is sent
    assert len(with_catalog) == (2 if sent else 0)


async def test_definer_warning_does_not_block_reading(settings: Settings, prepared: None) -> None:
    adapter = _adapter(settings)
    report, content = await adapter.snapshot(_target(settings, _role("definer")))
    assert report.findings == [] and report.warnings
    assert {item["name"] for item in content.tables} >= {"items", "mode", "slow"}


async def test_right_granted_after_clean_check_is_found_next_time(
    settings: Settings, prepared: None
) -> None:
    adapter = _adapter(settings)
    target = _target(settings, "cust_late")
    assert (await adapter.check(target)).findings == []
    admin = await _admin(settings, DATABASE)
    try:
        await admin.execute("GRANT INSERT ON shop.items TO cust_late")
    finally:
        await admin.close()
    with pytest.raises(DbRightsExcessError) as raised:
        await adapter.snapshot(target)
    assert raised.value.report is not None
    assert [item.kind for item in raised.value.report.findings] == ["write"]


async def test_every_access_is_repeatable_read_read_only(
    settings: Settings, prepared: None
) -> None:
    request = SampleRequest("shop", "mode", ["read_only", "isolation"])
    _, (rows,) = await _adapter(settings).sample(
        _target(settings, _role("reader")), [request], rows=5, preview=False
    )
    assert rows.rows == [["on", "repeatable read"]]


async def test_sample_reads_only_named_columns_and_drops_long_values(
    settings: Settings, prepared: None
) -> None:
    request = SampleRequest("shop", "items", ["id", "note"])
    _, (rows,) = await _adapter(settings, value_max_bytes=10).sample(
        _target(settings, _role("reader")), [request], rows=1, preview=False
    )
    assert rows.columns == ["id", "note"]
    assert rows.rows == [["1", None]]


async def test_slow_read_times_out_and_closes_the_connection(
    settings: Settings, prepared: None
) -> None:
    adapter = _adapter(settings, statement_timeout_seconds=1)
    with pytest.raises(DbUnavailableError) as raised:
        await adapter.sample(
            _target(settings, _role("reader")),
            [SampleRequest("shop", "slow", ["id"])],
            rows=1,
            preview=False,
        )
    assert raised.value.reason == "timeout"
    admin = await _admin(settings)
    try:
        async with asyncio.timeout(5):
            while await admin.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = 'cust_reader'"
            ):
                await asyncio.sleep(0.05)
    finally:
        await admin.close()


class _Proxy:
    """TCP-посредник к сокету тестового сервера: вход проходит, ответы на запросы — нет.

    `delay is None` — сервер «молчит»: ответы после входа теряются. Иначе первый ответ на
    каждую операцию задерживается на `delay` секунд — сервер отвечает, но медленно.
    """

    def __init__(self, path: str, delay: float | None) -> None:
        self.path = path
        self.delay = delay
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "_Proxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(self.path)
        asked = asyncio.Event()
        sent = 0

        async def to_server() -> None:
            nonlocal sent
            while chunk := await reader.read(65536):
                sent += 1
                if sent > 1:
                    asked.set()
                upstream_writer.write(chunk)
            upstream_writer.close()

        async def to_client() -> None:
            while chunk := await upstream_reader.read(65536):
                if sent > 1:
                    if self.delay is None:
                        continue
                    if asked.is_set():
                        asked.clear()
                        await asyncio.sleep(self.delay)
                writer.write(chunk)
            writer.close()

        tasks = [asyncio.create_task(to_server()), asyncio.create_task(to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            upstream_writer.close()
            writer.close()


async def _sessions_gone(settings: Settings, role: str) -> None:
    admin = await _admin(settings)
    try:
        async with asyncio.timeout(5):
            while await admin.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = $1", role
            ):
                await asyncio.sleep(0.05)
    finally:
        await admin.close()


# Задержка 0.2 с на каждый ответ: вызов asyncpg с подготовкой — два обмена, 0.4 < 0.5 с.
@pytest.mark.parametrize(("delay", "ends_by"), [(None, "client"), (0.2, "access")])
async def test_server_that_stops_answering_is_cut_off_by_portal_timeouts(
    settings: Settings,
    prepared: None,
    monkeypatch: pytest.MonkeyPatch,
    delay: float | None,
    ends_by: str,
) -> None:
    """Сервер принял соединение и запрос и молчит (или отвечает чуть быстрее срока на
    операцию): обращение кончается сроком портала с `timeout`, соединение разорвано,
    место `host:port` свободно (§7.6.5, «Сроки на стороне портала»)."""
    timeouts = {"client": 0.5, "access": 1.5}
    adapter = _adapter(
        settings,
        client_timeout_seconds=timeouts["client"],
        access_timeout_seconds=timeouts["access"],
        max_connections_per_server=1,
        wait_seconds=0.2,
    )
    target = _target(settings, _role("reader"))
    db = settings.database
    routed = asyncpg.connect
    proxied = True

    async def through_proxy(*args: Any, host: str, port: int, **kwargs: Any) -> Any:
        if proxied and host == ADDRESS:
            host, port = "127.0.0.1", proxy.port
        return await routed(*args, host=host, port=port, **kwargs)

    monkeypatch.setattr(asyncpg, "connect", through_proxy)
    async with _Proxy(f"{db.host}/.s.PGSQL.{db.port}", delay) as proxy:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(DbUnavailableError) as raised:
            await adapter.check(target)
        elapsed = loop.time() - started
        assert raised.value.reason == "timeout"
        if ends_by == "client":
            assert timeouts["client"] <= elapsed < timeouts["access"]
        else:
            assert timeouts["access"] <= elapsed < timeouts["access"] + timeouts["client"]
        await _sessions_gone(settings, _role("reader"))
    proxied = False
    assert (await adapter.check(target)).findings == []


async def test_third_access_to_one_server_waits_then_is_busy(
    settings: Settings, prepared: None
) -> None:
    adapter = _adapter(
        settings, max_connections_per_server=2, wait_seconds=1, statement_timeout_seconds=10
    )
    target = _target(settings, _role("reader"))

    async def slow() -> Any:
        return await adapter.sample(
            target, [SampleRequest("shop", "slow", ["id"])], rows=1, preview=False
        )

    first, second = asyncio.create_task(slow()), asyncio.create_task(slow())
    await asyncio.sleep(0.3)
    with pytest.raises(DbUnavailableError) as raised:
        await adapter.check(target)
    assert raised.value.reason == "busy"
    await asyncio.gather(first, second)


async def test_snapshot_keeps_keys_and_types_from_real_catalog_rows(
    settings: Settings, prepared: None
) -> None:
    """asyncpg отдаёт столбцы `"char"` (`contype`, `typtype`) байтами — ключи и типы не теряются."""
    _, content = await _adapter(settings).snapshot(_target(settings, _role("reader")))
    tables = {t["name"]: {c["name"]: c for c in t["columns"]} for t in content.tables}
    items, orders = tables["items"], tables["orders"]
    assert items["id"]["primary_key"] is True
    assert orders["item_id"]["primary_key"] is False
    assert orders["item_id"]["references"] == {"schema": "shop", "table": "items", "column": "id"}
    sandboxed = {name: column["sandbox_type"] for name, column in {**items, **orders}.items()}
    assert sandboxed == {
        "id": "integer",
        "name": "text",
        "note": "text",
        "item_id": "integer",
        "qty": "integer",
        "area": "numeric(12,2)",
        "made": "date",
    }


@pytest.mark.parametrize(
    ("declared", "sandboxed"),
    [
        ("integer", "integer"),
        ("numeric(12,2)", "numeric(12,2)"),
        ("character varying(20)[]", "character varying(20)[]"),
        ("geometry(MultiPolygon,4326)", "geometry(MultiPolygon,4326)"),
        ("cadastre.status_enum", "text"),
        ('"Weird"; DROP TABLE x', "text"),
        ("integer); DROP TABLE x; --", "text"),
    ],
)
def test_types_that_fail_reconciliation_become_text(declared: str, sandboxed: str) -> None:
    assert sandbox_type(declared) == sandboxed
