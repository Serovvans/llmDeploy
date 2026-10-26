"""Адаптер баз заказчика на настоящем PostgreSQL (docs/portal-api.md §7.6.5–7.6.8).

Роли с разными правами создаёт фикстура в тестовом сервере (`CLAUDE.md`, пайплайн «Код»,
правило 21); PostGIS здесь нет — геометрия проверяется на стенде
(`portal/dev/stand/test_customer_db.py`).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from portal.core.settings import DbSettings, Settings
from portal.databases.customer import AsyncpgCustomerDatabase, sandbox_type
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
}


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


def _target(settings: Settings, role: str) -> DbTarget:
    db = settings.database
    return DbTarget(
        uuid4(), db.host, db.port, DATABASE, role, "unused", "disable", ("shop", "public")
    )


def _adapter(settings: Settings, **update: Any) -> AsyncpgCustomerDatabase:
    config: DbSettings = settings.db.model_copy(update={"enabled": True, **update})
    return AsyncpgCustomerDatabase(config)


@pytest.mark.parametrize("name", list(ROLES))
async def test_rights_of_each_role(settings: Settings, prepared: None, name: str) -> None:
    report = await _adapter(settings).check(_target(settings, _role(name)))
    expected = ROLES[name][1]
    kinds = [item.kind for item in report.findings]
    if expected is None:
        assert kinds == []
    else:
        assert expected in kinds
    definers = [name for item in report.warnings for _, name in item.objects]
    assert definers == (["shop.recalc"] if name == "definer" else [])


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
