"""Помощники тестов подключений к базам заказчика (docs/portal-api.md §7.6).

`FakeCustomerDatabase` — подмена порта `CustomerDatabase`: снимок и строки образца
задаёт тест, каждый вызов запоминается (шаг 0 проверяется по их числу). Права роли и
запросы к настоящему PostgreSQL проверяет `tests/test_db_customer.py`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from portal.core.settings import Settings
from portal.databases.domain import RightsFinding
from portal.databases.ports import (
    DbRightsExcessError,
    DbTarget,
    RightsReport,
    SampleRequest,
    SampleRows,
    SnapshotContent,
)
from tests.support import Portal

PASSWORD = "Pa55-customer-secret-7c1e"
MARKER = "MarkerRow9d41"


def column(
    name: str,
    type_: str = "integer",
    *,
    primary_key: bool = False,
    comment: str | None = None,
    geometry: dict[str, Any] | None = None,
    sandbox_type: str | None = None,
    binary: bool = False,
    references: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Столбец снимка в виде, который сохраняет адаптер (§7.6.7)."""
    return {
        "name": name,
        "type": type_,
        "not_null": primary_key,
        "primary_key": primary_key,
        "references": references,
        "comment": comment,
        "geometry": geometry,
        "sandbox_type": sandbox_type or type_,
        "binary": binary,
    }


def table(
    schema: str, name: str, columns: Sequence[dict[str, Any]], comment: str | None = None
) -> dict[str, Any]:
    """Таблица снимка."""
    return {
        "schema": schema,
        "name": name,
        "kind": "table",
        "comment": comment,
        "columns": list(columns),
    }


TABLES = [
    table(
        "cadastre",
        "parcels",
        [
            column("id", primary_key=True),
            column("status", "text"),
            column("area", "numeric(12,2)"),
            column(
                "geom",
                "geometry(MultiPolygon,4326)",
                geometry={"type": "MultiPolygon", "srid": 4326},
                sandbox_type="text",
            ),
        ],
        comment="Земельные участки",
    ),
    table(
        "cadastre",
        "owners",
        [
            column("id", primary_key=True),
            column("phone", "text"),
            column("scan", "bytea", binary=True),
        ],
    ),
    table("public", "notes", [column("id", primary_key=True), column("body", "text")]),
]
SHAPE = (
    '{"type":"MultiPolygon","coordinates":[[[[55.1,51.7],[55.2,51.7],[55.2,51.8],[55.1,51.7]]]]}'
)
ROWS: dict[tuple[str, str], list[dict[str, str | None]]] = {
    ("cadastre", "parcels"): [
        {
            "id": "1",
            "status": MARKER,
            "area": "10.50",
            "geom": SHAPE,
        },
        {"id": "2", "status": "Аренда", "area": None, "geom": None},
    ],
    ("cadastre", "owners"): [{"id": "1", "phone": "+7 900"}],
    ("public", "notes"): [{"id": "1", "body": "Заметка"}],
}


@dataclass
class FakeCustomerDatabase:
    """Подменная база заказчика: итог проверки прав по сценарию, вызовы — в `calls`."""

    findings: list[RightsFinding] = field(default_factory=list)
    warnings: list[RightsFinding] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=lambda: list(TABLES))
    rows: dict[tuple[str, str], list[dict[str, str | None]]] = field(
        default_factory=lambda: dict(ROWS)
    )
    calls: list[tuple[str, Any]] = field(default_factory=list)
    targets: list[DbTarget] = field(default_factory=list)

    def _report(self) -> RightsReport:
        return RightsReport(list(self.findings), list(self.warnings))

    async def check(self, target: DbTarget) -> RightsReport:
        self.calls.append(("check", None))
        self.targets.append(target)
        return self._report()

    async def snapshot(self, target: DbTarget) -> tuple[RightsReport, SnapshotContent]:
        self.calls.append(("snapshot", None))
        self.targets.append(target)
        if self.findings:
            raise DbRightsExcessError(self._report())
        return self._report(), SnapshotContent(list(self.tables), False, "public")

    async def sample(
        self, target: DbTarget, tables: Sequence[SampleRequest], *, rows: int, preview: bool
    ) -> tuple[RightsReport, Sequence[SampleRows]]:
        self.calls.append(
            ("sample", [(item.table, tuple(item.columns), item.geometry) for item in tables])
        )
        self.targets.append(target)
        if self.findings:
            raise DbRightsExcessError(self._report())
        result = []
        for item in tables:
            found = self.rows.get((item.schema, item.table), [])[:rows]
            values = [[row.get(name) for name in item.columns] for row in found]
            shapes = (
                [row.get(item.geometry) for row in found] if item.geometry else [None] * len(found)
            )
            result.append(
                SampleRows(list(item.columns), values, shapes if preview else None, False)
            )
        return self._report(), result


def db_settings(settings: Settings, **db: Any) -> Settings:
    """Настройки с включёнными подключениями к базам (`db.enabled`)."""
    section = settings.db.model_copy(update={"enabled": True, **db})
    return settings.model_copy(update={"db": section})


async def admin_client(portal: Portal, login: str = "boss") -> httpx.AsyncClient:
    """Клиент администратора, прошедшего вход."""
    client = portal.client()
    await portal.onboard(client, login, "admin")
    return client


async def create_connection(
    admin: httpx.AsyncClient, *, user_ids: Sequence[str] | None = None, **fields: Any
) -> dict[str, Any]:
    """Подключение к «кадастру» с образцами; доступ — всем или списку `user_ids`."""
    access = {"all": True} if user_ids is None else {"all": False, "user_ids": list(user_ids)}
    body = {
        "title": "Кадастр",
        "host": "db.customer.test",
        "database": "cadastre",
        "username": "portal_reader",
        "password": PASSWORD,
        "tls_mode": "disable",
        "schemas": ["cadastre", "public"],
        "samples_allowed": True,
        "access": access,
        **fields,
    }
    response = await admin.post("/api/admin/db-connections", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def refresh(client: httpx.AsyncClient, connection_id: str) -> httpx.Response:
    """«Обновить схему» от имени сотрудника."""
    return await client.post(f"/api/db/connections/{connection_id}/snapshot")


SELECTION = [{"schema": "cadastre", "name": "parcels"}, {"schema": "public", "name": "notes"}]
