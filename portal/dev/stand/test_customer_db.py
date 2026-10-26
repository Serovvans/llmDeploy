"""Тестовая база customer-db на поднятом стенде: права ролей, журнал операторов, слой.

Требования — docs/portal-api.md §13.5 и обязательные тесты 1.0 (§13.1): обращения портала
через порт `CustomerDatabase` из контейнера `portal-api` — в журнале операторов базы у
`scancontrol-portal` только запросы портала внутри `BEGIN … READ ONLY` … `ROLLBACK`; из
песочницы `sql-sandbox` до базы соединения нет; слой предпросмотра — GeoJSON в WGS 84.
Поведение сценариев (шаг 0, изоляция, строки не в истории) проверяют тесты бэкенда.

Запуск: `uv run pytest stand` при поднятом стенде (make dev-up). Без сервиса customer-db
проверки пропускаются. Пароль ролей — `CUSTOMER_DB_PASSWORD` из окружения или из
`portal/dev/.env`; в контейнер он передаётся через `docker compose exec -e`.
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stand.support import compose, docker, ok

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
APPLICATION = "scancontrol-portal"
ROLES = (
    "portal_reader",
    "portal_writer",
    "portal_owner",
    "portal_writeall",
    "portal_super",
    "portal_setrole",
    "portal_definer",
)
# Итог проверки прав по таблице ролей §13.5: среди находок есть этот вид.
EXPECTED_KIND = {
    "portal_writer": "write",
    "portal_owner": "owner",
    "portal_writeall": "predefined_role",
    "portal_super": "superuser",
    "portal_setrole": "write",
}
# Обращения портала через порт: права каждой роли, снимок, образец и слой у читателя.
SCRIPT = """
import asyncio, json, os, sys, uuid
from portal.core.settings import load_settings
from portal.databases.customer import AsyncpgCustomerDatabase
from portal.databases.ports import DbTarget, SampleRequest

ROLES = json.loads(sys.argv[1])


def target(role):
    return DbTarget(
        uuid.uuid4(), "customer-db", 5432, "cadastre", role,
        os.environ["CUSTOMER_DB_PASSWORD"], "disable", ("cadastre", "public"),
    )


async def main():
    database = AsyncpgCustomerDatabase(load_settings(os.environ).db)
    rights = {}
    for role in ROLES:
        report = await database.check(target(role))
        rights[role] = {
            "findings": [item.kind for item in report.findings],
            "warnings": [name for item in report.warnings for _, name in item.objects],
        }
    _, snapshot = await database.snapshot(target("portal_reader"))
    parcels = SampleRequest("cadastre", "parcels", ["id", "cadastral_number", "status", "area"])
    _, (sample,) = await database.sample(target("portal_reader"), [parcels], rows=5, preview=False)
    layer = SampleRequest("cadastre", "parcels", ["id", "status"], "geom")
    _, (preview,) = await database.sample(
        target("portal_reader"), [layer], rows=50, preview=True
    )
    print(json.dumps({
        "rights": rights,
        "tables": sorted(f"{item['schema']}.{item['name']}" for item in snapshot.tables),
        "postgis": snapshot.postgis_schema,
        "sample_rows": len(sample.rows),
        "preview_rows": len(preview.rows),
        "geojson": list(preview.geojson or []),
    }))


asyncio.run(main())
"""


def _password() -> str:
    value = os.environ.get("CUSTOMER_DB_PASSWORD")
    if value:
        return value
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, found = line.partition("=")
            if name.strip() == "CUSTOMER_DB_PASSWORD" and found.strip():
                return found.strip().strip("'\"")
    pytest.skip("CUSTOMER_DB_PASSWORD не задан")


@pytest.fixture(scope="module")
def customer_db() -> str:
    found = compose("ps", "-q", "customer-db")
    if found.returncode != 0 or not found.stdout.strip():
        pytest.skip("сервис customer-db не запущен")
    return found.stdout.strip()


@pytest.fixture(scope="module")
def accessed(customer_db: str) -> tuple[dict[str, Any], list[str]]:
    """Итог обращений портала и строки журнала базы за время этих обращений."""
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    result = compose(
        "exec",
        "-T",
        "-e",
        f"CUSTOMER_DB_PASSWORD={_password()}",
        "portal-api",
        "python",
        "-c",
        SCRIPT,
        json.dumps(ROLES),
        timeout=300,
    )
    data = json.loads(ok(result).splitlines()[-1])
    logs = docker("logs", "--since", started, customer_db)
    assert logs.returncode == 0, logs.stderr
    return data, (logs.stdout + logs.stderr).splitlines()


def test_rights_of_each_stand_role(accessed: tuple[dict[str, Any], list[str]]) -> None:
    rights = accessed[0]["rights"]
    for role in ROLES:
        expected = EXPECTED_KIND.get(role)
        if expected is None:
            assert rights[role]["findings"] == [], role
        else:
            assert expected in rights[role]["findings"], role
    assert "cadastre.recalc_area" in " ".join(rights["portal_definer"]["warnings"])
    # Предупреждение о функции — только у роли, которой выдан EXECUTE.
    assert not any("recalc_area" in name for name in rights["portal_reader"]["warnings"])


def test_snapshot_and_sample_through_the_port(accessed: tuple[dict[str, Any], list[str]]) -> None:
    data = accessed[0]
    assert {"cadastre.parcels", "cadastre.leases", "cadastre.owners", "public.notes"} <= set(
        data["tables"]
    )
    assert data["postgis"] == "public"
    assert data["sample_rows"] == 5


def test_preview_layer_is_geojson_in_wgs84(accessed: tuple[dict[str, Any], list[str]]) -> None:
    data = accessed[0]
    # 60 строк в таблице, предел — 50.
    assert data["preview_rows"] == 50
    shapes = [json.loads(item) for item in data["geojson"]]
    assert shapes and all(shape["type"] == "MultiPolygon" for shape in shapes)
    for shape in shapes:
        for ring in shape["coordinates"][0]:
            for lon, lat in ring:
                assert 55 < lon < 56 and 51 < lat < 52


def _statements(lines: list[str]) -> list[str]:
    """Операторы сеансов портала: `statement:` и `execute …:` с префиксом `%a %u`.

    Многострочный оператор занимает несколько строк журнала: продолжения идут без
    префикса (PostgreSQL начинает их табуляцией), поэтому запись собирается до начала
    следующей — строки вида `<приложение> <роль> <УРОВЕНЬ>:`, где оба поля могут быть пусты.
    """
    record_start = re.compile(r"^\S* \S* [A-Z]+:")
    records: list[str] = []
    for line in lines:
        if record_start.match(line) or not records:
            records.append(line)
        else:
            records[-1] += "\n" + line
    pattern = re.compile(
        rf"^{APPLICATION} \S+ LOG:\s+(?:statement|execute [^:]*): (.*)$", re.DOTALL
    )
    found = []
    for record in records:
        match = pattern.match(record)
        if match:
            found.append(match.group(1).strip())
    return found


def test_statements_parse_asyncpg_leading_newline() -> None:
    """Разбор журнала без стенда: asyncpg шлёт оператор с ведущего перевода строки."""
    lines = [
        f"{APPLICATION} portal_reader LOG:  statement: BEGIN READ ONLY",
        f"{APPLICATION} portal_reader LOG:  execute __asyncpg_stmt_1__: ",
        "\tSELECT n.nspname",
        "\tFROM pg_namespace n",
        f"{APPLICATION} portal_reader DETAIL:  parameters: $1 = 'cadastre'",
        f"{APPLICATION} portal_reader LOG:  execute __asyncpg_stmt_2__: ",
        "\t",
        "\tWITH t AS (SELECT 1) SELECT * FROM t",
        "psql postgres LOG:  statement: DROP TABLE cadastre.leases",
        "  LOG:  checkpoint starting: time",
        f"{APPLICATION} portal_reader LOG:  statement: ROLLBACK",
    ]
    assert _statements(lines) == [
        "BEGIN READ ONLY",
        "SELECT n.nspname\n\tFROM pg_namespace n",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "ROLLBACK",
    ]


def test_only_portal_queries_in_read_only_transactions(
    accessed: tuple[dict[str, Any], list[str]],
) -> None:
    statements = _statements(accessed[1])
    assert statements, "в журнале customer-db нет операторов портала: включён ли log_statement?"
    open_transaction = False
    for statement in statements:
        upper = statement.upper()
        if upper.startswith("BEGIN"):
            assert not open_transaction
            assert "READ ONLY" in upper, statement
            open_transaction = True
        elif upper.startswith("ROLLBACK"):
            assert open_transaction
            open_transaction = False
        else:
            # Внутри транзакции — только чтение запросами портала (§7.6.5–7.6.8).
            assert open_transaction, statement
            assert upper.startswith(("SELECT", "WITH")), statement
    assert not open_transaction


def test_sql_sandbox_cannot_reach_customer_db(customer_db: str) -> None:
    def sandbox_sh(script: str) -> int:
        return compose("exec", "-T", "sql-sandbox", "bash", "-c", script).returncode

    assert sandbox_sh("getent hosts customer-db") != 0
    address = ok(
        docker(
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
            customer_db,
        )
    ).split()[0]
    assert sandbox_sh(f"timeout 5 bash -c 'exec 3<>/dev/tcp/{address}/5432'") != 0
