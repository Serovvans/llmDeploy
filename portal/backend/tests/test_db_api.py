"""Подключения к базам заказчика через API (docs/portal-api.md §7.6): доступ, пароль, шаг 0.

База заказчика подменена (`FakeCustomerDatabase`): тесты смотрят, что и сколько раз портал
у неё спросил. Права ролей на настоящем PostgreSQL — `tests/test_db_customer.py`.
"""

import logging
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from portal.core.settings import Settings
from portal.databases.domain import finding
from portal.databases.ports import DbTarget, RightsReport
from tests.conftest import close_portal, make_portal
from tests.db_support import (
    PASSWORD,
    SELECTION,
    FakeCustomerDatabase,
    admin_client,
    create_connection,
    db_settings,
    refresh,
)
from tests.support import Portal, new_tool_dialog

pytestmark = pytest.mark.anyio

WRITE = finding("write", [("table", "cadastre.leases")], ["INSERT"])


@pytest.fixture
async def fake() -> FakeCustomerDatabase:
    return FakeCustomerDatabase()


@pytest.fixture
async def portal(settings: Settings, fake: FakeCustomerDatabase) -> AsyncIterator[Portal]:
    started = await make_portal(db_settings(settings), customer_database=fake)
    yield started
    await close_portal(started)


async def test_password_is_never_returned_logged_or_stored_in_clear(
    portal: Portal, fake: FakeCustomerDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    admin = await admin_client(portal)
    created = await create_connection(admin)
    target = created["id"]
    bodies = [
        str(created),
        (await admin.get("/api/admin/db-connections")).text,
        (
            await admin.patch(f"/api/admin/db-connections/{target}", json={"password": PASSWORD})
        ).text,
        (await admin.post(f"/api/admin/db-connections/{target}/check")).text,
    ]
    employee = await portal.employee()
    bodies.append((await refresh(employee, target)).text)
    bodies.append((await employee.get("/api/db/connections")).text)
    assert all(PASSWORD not in body for body in bodies)
    assert "password" not in created
    # Порт получил пароль расшифрованным, но в `repr` его нет.
    assert fake.targets[0].password == PASSWORD and PASSWORD not in repr(fake.targets[0])
    assert PASSWORD not in str(await portal.audit_events())
    assert PASSWORD not in caplog.text
    stored = (await portal.rows("SELECT password_encrypted FROM db_connections"))[0][0]
    assert PASSWORD.encode() not in bytes(stored)
    cipher = portal.container.databases._cipher
    assert cipher.decrypt(UUID(target), bytes(stored)) == PASSWORD
    with pytest.raises(Exception):  # noqa: B017 — AAD чужого подключения
        cipher.decrypt(UUID(int=1), bytes(stored))


async def test_employee_gets_403_on_admin_routes(portal: Portal) -> None:
    admin = await admin_client(portal)
    target = (await create_connection(admin))["id"]
    employee = await portal.employee()
    for method, path in [
        ("GET", "/api/admin/db-connections"),
        ("POST", "/api/admin/db-connections"),
        ("PATCH", f"/api/admin/db-connections/{target}"),
        ("DELETE", f"/api/admin/db-connections/{target}"),
        ("POST", f"/api/admin/db-connections/{target}/check"),
    ]:
        response = await employee.request(method, path, json={})
        assert response.status_code == 403, (method, path)


async def test_employee_outside_access_list_sees_nothing(portal: Portal) -> None:
    admin = await admin_client(portal)
    employee = await portal.employee()
    other = await portal.employee("petrov")
    allowed = str(await portal.user_id("petrov"))
    target = (await create_connection(admin, user_ids=[allowed]))["id"]
    assert (await refresh(other, target)).status_code == 200

    assert (await employee.get("/api/db/connections")).json()["items"] == []
    assert (await employee.get(f"/api/db/connections/{target}/snapshot")).status_code == 404
    assert (await refresh(employee, target)).status_code == 404
    dialog = await new_tool_dialog(employee, "sql")
    response = await employee.patch(
        f"/api/dialogs/{dialog}", json={"db": {"connection_id": target, "tables": SELECTION}}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "db_connection_not_found",
    )


async def test_recorded_excess_stops_before_the_port(
    portal: Portal, fake: FakeCustomerDatabase
) -> None:
    admin = await admin_client(portal)
    target = (await create_connection(admin))["id"]
    employee = await portal.employee()
    assert (await refresh(employee, target)).status_code == 200

    fake.findings = [WRITE]
    checked = await admin.post(f"/api/admin/db-connections/{target}/check")
    assert checked.status_code == 200
    assert checked.json()["rights"]["status"] == "excess"
    assert checked.json()["rights"]["findings"][0]["kind"] == "write"

    # Шаг 0: записанный `excess` — отказ без обращения к базе.
    calls = len(fake.calls)
    portal.clock.advance(hours=1)
    response = await refresh(employee, target)
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "db_connection_unusable",
    )
    assert len(fake.calls) == calls
    # Проверка администратора идёт к базе и без находок снимает `excess`.
    fake.findings = []
    cleared = await admin.post(f"/api/admin/db-connections/{target}/check")
    assert cleared.json()["rights"]["status"] == "read_only"
    assert (await refresh(employee, target)).status_code == 200


async def test_login_change_clears_recorded_excess(
    portal: Portal, fake: FakeCustomerDatabase
) -> None:
    admin = await admin_client(portal)
    target = (await create_connection(admin))["id"]
    fake.findings = [WRITE]
    await admin.post(f"/api/admin/db-connections/{target}/check")
    changed = await admin.patch(
        f"/api/admin/db-connections/{target}", json={"username": "portal_other"}
    )
    assert changed.json()["rights"]["status"] == "unknown"


async def test_stale_result_does_not_overwrite_login_change(
    portal: Portal, fake: FakeCustomerDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Итог обращения со старой ролью не затирает `unknown` после смены параметров входа."""
    admin = await admin_client(portal)
    target = (await create_connection(admin))["id"]
    fake.findings = [WRITE]
    check = fake.check

    async def check_while_admin_switches_role(db_target: DbTarget) -> RightsReport:
        portal.clock.advance(seconds=1)
        changed = await admin.patch(
            f"/api/admin/db-connections/{target}", json={"username": "portal_clean"}
        )
        assert changed.json()["rights"]["status"] == "unknown"
        return await check(db_target)

    monkeypatch.setattr(fake, "check", check_while_admin_switches_role)
    await admin.post(f"/api/admin/db-connections/{target}/check")

    rows = await portal.rows("SELECT rights_status, username FROM db_connections")
    assert [tuple(row) for row in rows] == [("unknown", "portal_clean")]


async def test_flag_off_closes_routes_and_hides_selection(
    settings: Settings, fake: FakeCustomerDatabase
) -> None:
    disabled = await make_portal(settings, customer_database=fake)
    try:
        admin = await admin_client(disabled)
        employee = await disabled.employee()
        for client, path in [
            (admin, "/api/admin/db-connections"),
            (employee, "/api/db/connections"),
            (employee, f"/api/db/connections/{UUID(int=5)}/snapshot"),
        ]:
            assert (await client.get(path)).status_code == 403, path
        dialog = await new_tool_dialog(employee, "sql")
        # Выбор, сохранённый при включённом флаге, не отдаётся и не меняется.
        await disabled.execute(
            "UPDATE dialogs SET db_connection_title = 'Кадастр', db_tables = '[]'::jsonb"
        )
        assert (await employee.get(f"/api/dialogs/{dialog}")).json()["db"] is None
        patch = await employee.patch(f"/api/dialogs/{dialog}", json={"db": None})
        assert patch.status_code == 403
        assert (await employee.get("/api/config")).json()["db"]["enabled"] is False
        assert fake.calls == []
    finally:
        await close_portal(disabled)
